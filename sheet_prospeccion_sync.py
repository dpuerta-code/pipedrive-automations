#!/usr/bin/env python3
"""
Mantiene al día la tabla ClickHouse bpa.sheet_prospeccion_matches (la lista de
empresas de la hoja de prospección "Prospección 2 T1-T9" cruzada contra
Pipedrive), sin escribir nada en Pipedrive.

Dos tareas, cada corrida:

1. Quitar de la lista las organizaciones matcheadas (is_match=1) cuyo label
   en Pipedrive ya es "Blocked – Already Assigned" (ya se prospectaron /
   quedaron asignadas a otro proceso, ya no deben seguir boosteando el
   scoring).

2. Buscar, entre las organizaciones creadas en Pipedrive desde NEW_ORG_CUTOFF
   en adelante, si alguna coincide con una empresa que sigue como
   "Posible New" (is_match=0) en la hoja — por nombre exacto, o por dominio
   de email + país (usando la columna `domain` ya poblada en la tabla).
   Si encuentra un candidato único y confiable, actualiza is_match=1,
   org_id y org_name. Casos ambiguos (más de un candidato) se listan en el
   log para revisión manual, no se tocan.

Nunca escribe notas ni nada en Pipedrive — solo mantiene la tabla ClickHouse.

TEST_MODE=true → solo imprime lo que haría, no modifica la tabla.
Cron GitHub Actions: cada 2 días.
"""

import os
import re

import clickhouse_connect

CLICKHOUSE_HOST = os.environ["CLICKHOUSE_HOST"]
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8123"))
CLICKHOUSE_USER = os.environ["CLICKHOUSE_USER"]
CLICKHOUSE_PASSWORD = os.environ["CLICKHOUSE_PASSWORD"]
CLICKHOUSE_SECURE = os.environ.get("CLICKHOUSE_SECURE", "false").lower() == "true"

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

BLOCKED_LABEL = "Blocked – Already Assigned"

# Solo se consideran orgs creadas desde esta fecha en adelante para el
# matching de "Posible New" -> matched (pedido explícito: "de hoy en
# adelante", fijado al día en que se configuró este script).
NEW_ORG_CUTOFF = "2026-08-25 00:00:00"

COUNTRY_ABBR = {
    "MX": "Mexico", "CO": "Colombia", "CL": "Chile", "PA": "Panama",
    "CR": "Costa Rica", "GT": "Guatemala", "PE": "Peru", "BR": "Brazil",
    "EC": "Ecuador", "DO": "Dominican Republic", "HN": "Honduras",
    "SV": "El Salvador", "NI": "Nicaragua", "UY": "Uruguay", "PY": "Paraguay",
    "AR": "Argentina", "BO": "Bolivia", "VE": "Venezuela",
}


def norm_country(value):
    if not value:
        return None
    v = value.strip()
    v = COUNTRY_ABBR.get(v.upper(), v)
    v = v.replace("México", "Mexico").replace("Panamá", "Panama")
    return v.strip().lower()


def get_client():
    return clickhouse_connect.get_client(
        host=CLICKHOUSE_HOST,
        port=CLICKHOUSE_PORT,
        username=CLICKHOUSE_USER,
        password=CLICKHOUSE_PASSWORD,
        secure=CLICKHOUSE_SECURE,
    )


def remove_blocked_orgs(client):
    rows = client.query(f"""
        SELECT spm.sheet_company, spm.org_id, op.name
        FROM bpa.sheet_prospeccion_matches spm
        INNER JOIN bpa.organizations_custom_fields_pivoted AS op FINAL
            ON spm.org_id = op.organization_id
        WHERE spm.is_match = 1
          AND op.label = '{BLOCKED_LABEL}'
    """).result_rows

    if not rows:
        print("Paso 1 (orgs bloqueadas): ninguna para quitar.")
        return []

    print(f"Paso 1 (orgs bloqueadas): {len(rows)} organizacion(es) a quitar de la lista:")
    for sheet_company, org_id, org_name in rows:
        print(f"  - {sheet_company} -> org {org_id} ({org_name})")

    if TEST_MODE:
        print("  [TEST MODE] no se modifica la tabla.")
        return rows

    companies = ",".join("'" + c.replace("'", "''") + "'" for c, _, _ in rows)
    client.command(f"""
        ALTER TABLE bpa.sheet_prospeccion_matches
        DELETE WHERE sheet_company IN ({companies})
    """)
    print(f"  Quitadas {len(rows)} fila(s) de bpa.sheet_prospeccion_matches.")
    return rows


def find_name_candidates(client, company):
    escaped = company.replace("'", "''")
    return client.query(f"""
        SELECT toInt64(id), name
        FROM pipedrive.organizations
        WHERE lower(trim(name)) = lower(trim('{escaped}'))
          AND add_time >= '{NEW_ORG_CUTOFF}'
    """).result_rows


def find_domain_candidates(client, domain, sheet_country):
    escaped_domain = domain.replace("'", "''")
    rows = client.query(f"""
        SELECT DISTINCT toInt64(p.org_id) AS org_id, op.name, op.country
        FROM pipedrive.persons p
        INNER JOIN pipedrive.organizations o ON o.id = toFloat64(p.org_id)
        LEFT JOIN bpa.organizations_custom_fields_pivoted AS op FINAL
            ON op.organization_id = toInt64(p.org_id)
        ARRAY JOIN p.email AS e
        WHERE p.org_id IS NOT NULL
          AND p.delete_time IS NULL
          AND lower(JSONExtractString(e, 'value')) LIKE '%@{escaped_domain}'
          AND o.add_time >= '{NEW_ORG_CUTOFF}'
    """).result_rows

    if not sheet_country:
        return rows

    wanted = norm_country(sheet_country)
    filtered = [r for r in rows if not r[2] or norm_country(r[2]) == wanted]
    return filtered if filtered else rows


def match_new_orgs(client):
    unmatched = client.query("""
        SELECT sheet_company, country, domain
        FROM bpa.sheet_prospeccion_matches
        WHERE is_match = 0
    """).result_rows

    print(f"\nPaso 2 (orgs nuevas vs Posible New): {len(unmatched)} empresa(s) sin match a revisar.")

    matched = []
    ambiguous = []

    for sheet_company, country, domain in unmatched:
        name_candidates = find_name_candidates(client, sheet_company)

        if len(name_candidates) == 1:
            org_id, org_name = name_candidates[0]
            matched.append((sheet_company, org_id, org_name, "nombre exacto"))
            continue
        if len(name_candidates) > 1:
            ambiguous.append((sheet_company, f"{len(name_candidates)} candidatos por nombre"))
            continue

        if not domain:
            continue

        domain_candidates = find_domain_candidates(client, domain, country)
        if len(domain_candidates) == 1:
            org_id, org_name, _ = domain_candidates[0]
            matched.append((sheet_company, org_id, org_name, f"dominio {domain}"))
        elif len(domain_candidates) > 1:
            ambiguous.append((sheet_company, f"{len(domain_candidates)} candidatos por dominio {domain}"))

    if not matched:
        print("  Ningun match nuevo encontrado.")
    else:
        print(f"  {len(matched)} match(es) nuevo(s) encontrado(s):")
        for sheet_company, org_id, org_name, why in matched:
            print(f"    - {sheet_company} -> org {org_id} ({org_name}) [{why}]")

    if ambiguous:
        print(f"  {len(ambiguous)} caso(s) ambiguo(s) (sin tocar, revisar manualmente):")
        for sheet_company, why in ambiguous:
            print(f"    - {sheet_company}: {why}")

    if matched and not TEST_MODE:
        for sheet_company, org_id, org_name, _ in matched:
            escaped_company = sheet_company.replace("'", "''")
            escaped_name = org_name.replace("'", "''") if org_name else ""
            client.command(f"""
                ALTER TABLE bpa.sheet_prospeccion_matches
                UPDATE org_id = {org_id}, org_name = '{escaped_name}', is_match = 1
                WHERE sheet_company = '{escaped_company}'
            """)
        print(f"  Tabla actualizada con {len(matched)} nuevo(s) match(es).")
    elif matched and TEST_MODE:
        print("  [TEST MODE] no se modifica la tabla.")

    return matched, ambiguous


def main():
    print(f"\n{'='*60}")
    print("Sheet Prospeccion Sync (Prospección 2 T1-T9)")
    if TEST_MODE:
        print("MODO TEST: solo lectura, no se modifica bpa.sheet_prospeccion_matches")
    print(f"{'='*60}\n")

    client = get_client()

    removed = remove_blocked_orgs(client)
    matched, ambiguous = match_new_orgs(client)

    print(f"\n{'='*60}")
    print(f"Resumen: {len(removed)} quitadas por bloqueo, {len(matched)} nuevas matcheadas, "
          f"{len(ambiguous)} ambiguas sin resolver")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
