#!/usr/bin/env python3
"""
org_fcd_sweep.py

Mantiene bpa.org_fcd_corrections (ClickHouse) al día. Esa tabla es la
corrección manual para un bug estructural del pipeline Airbyte/dbt: cuando un
campo custom de Pipedrive se vacía (pasa a null), ese clear NUNCA se
sincroniza a ClickHouse — el valor viejo se queda pegado indefinidamente,
aunque otros campos de la misma fila sí se actualicen.

Dos partes, en este orden:

  1. Revalidar correcciones existentes (corrected_value IS NULL): si el sync
     finalmente se puso al día y Pipedrive ya tiene un valor real para ese
     campo, la corrección quedó obsoleta — se borra.
  2. Buscar organizaciones nuevas: orgs con lead activo donde ClickHouse cree
     que First Contact Date tiene valor, pero nunca se verificó contra
     Pipedrive en vivo. Si el campo está vacío ahí, se inserta la corrección.

Env vars requeridas:
  PIPEDRIVE_API_TOKEN
  CLICKHOUSE_HOST       (e.g. sql-clickhouse.clickhouse.com)
  CLICKHOUSE_PORT       (default 8443)
  CLICKHOUSE_USER
  CLICKHOUSE_PASSWORD
  TEST_MODE             (default false → solo loguea, no inserta/borra)
"""

import json
import os
import time
import requests
from datetime import datetime

# ── Pipedrive ──────────────────────────────────────────────────────────────
PD_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
PD_BASE = "https://slang.pipedrive.com/api/v1"

FCD_KEY = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"  # First Contact Date

# ── ClickHouse ─────────────────────────────────────────────────────────────
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8443")
CH_USER = os.environ.get("CLICKHOUSE_USER", "s_puerta")
CH_PASS = os.environ.get("CLICKHOUSE_PASSWORD", "")
CH_BASE = f"https://{CH_HOST}:{CH_PORT}/"

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# ── Pipedrive rate limiting (78 req / 10s) ─────────────────────────────────
_req_count = 0
_req_win = time.time()


def _rate_limit():
    global _req_count, _req_win
    _req_count += 1
    if _req_count >= 78:
        elapsed = time.time() - _req_win
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        _req_count = 0
        _req_win = time.time()


def pd_get_org(org_id):
    _rate_limit()
    r = requests.get(
        f"{PD_BASE}/organizations/{org_id}",
        params={"api_token": PD_TOKEN},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json().get("data") or {})


# ── ClickHouse HTTP ────────────────────────────────────────────────────────
def ch_get(sql):
    r = requests.get(
        CH_BASE,
        params={"query": sql + " FORMAT JSONEachRow"},
        auth=(CH_USER, CH_PASS),
        timeout=60,
    )
    r.raise_for_status()
    rows = []
    for line in r.text.strip().splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def ch_execute(sql):
    """DDL/DML sin cuerpo de datos (DELETE, INSERT ... VALUES literal, etc.)."""
    r = requests.post(
        CH_BASE,
        params={"query": sql},
        auth=(CH_USER, CH_PASS),
        timeout=60,
    )
    r.raise_for_status()
    return r.text


def sql_escape(s):
    return (s or "").replace("'", "''").replace("\n", " ").replace("\r", " ")


# ── Parte 1: revalidar correcciones existentes ─────────────────────────────
def revalidate_existing_corrections():
    print("\n--- Parte 1: revalidando correcciones existentes ---")
    rows = ch_get(
        "SELECT organization_id, note FROM bpa.org_fcd_corrections FINAL "
        "WHERE corrected_value IS NULL ORDER BY organization_id"
    )
    print(f"Correcciones activas a revisar: {len(rows)}")

    obsoletas = []
    for row in rows:
        org_id = int(row["organization_id"])
        org = pd_get_org(org_id)
        fcd = org.get(FCD_KEY)
        if fcd:
            obsoletas.append((org_id, org.get("name") or str(org_id), fcd))

    print(f"Obsoletas encontradas (ya tienen valor real en Pipedrive): {len(obsoletas)}")
    for org_id, name, fcd in obsoletas:
        print(f"  BORRAR [{org_id}] {name} — ahora tiene {fcd}")

    if obsoletas and not TEST_MODE:
        ids = ", ".join(str(o[0]) for o in obsoletas)
        ch_execute(f"DELETE FROM bpa.org_fcd_corrections WHERE organization_id IN ({ids})")
    elif obsoletas:
        print("[TEST] No se borró nada.")

    return len(rows), obsoletas


# ── Parte 2: buscar casos nuevos ────────────────────────────────────────────
def find_new_corrections():
    print("\n--- Parte 2: buscando organizaciones nuevas ---")
    candidate_sql = """
    SELECT DISTINCT l.organization_id
    FROM pipedrive.leads l
    LEFT JOIN bpa.organizations_custom_fields_pivoted ocf FINAL
        ON ocf.organization_id = l.organization_id
    WHERE l.is_archived = 0
      AND l.organization_id IS NOT NULL
      AND ocf.first_contact_date IS NOT NULL AND ocf.first_contact_date != ''
      AND l.organization_id NOT IN (SELECT organization_id FROM bpa.org_fcd_corrections)
    """
    rows = ch_get(candidate_sql)
    candidate_ids = [int(r["organization_id"]) for r in rows]
    print(f"Candidatas a revisar: {len(candidate_ids)}")

    nuevas = []
    for org_id in candidate_ids:
        org = pd_get_org(org_id)
        if not org:
            continue
        fcd = org.get(FCD_KEY)
        if fcd:
            continue  # tiene valor real, no es un caso del bug

        owner = org.get("owner_id")
        owner_id = owner.get("id") if isinstance(owner, dict) else owner
        name = org.get("name") or str(org_id)
        nuevas.append((org_id, name, owner_id))

    print(f"Nuevas correcciones a insertar: {len(nuevas)}")
    for org_id, name, owner_id in nuevas:
        print(f"  INSERT [{org_id}] {name}")

    if nuevas and not TEST_MODE:
        today = datetime.now().strftime("%Y-%m-%d")
        values = []
        for org_id, name, owner_id in nuevas:
            owner_sql = f"'{owner_id}'" if owner_id else "NULL"
            note = sql_escape(
                f"sweep diario {today}: {name} - first contact date vacio en "
                f"pipedrive, clear nunca sincronizo (bug pipeline)"
            )
            values.append(f"({org_id}, NULL, {owner_sql}, '{note}')")
        sql = (
            "INSERT INTO bpa.org_fcd_corrections "
            "(organization_id, corrected_value, bdr, note) VALUES "
            + ",\n".join(values)
        )
        ch_execute(sql)
    elif nuevas:
        print("[TEST] No se insertó nada.")

    return len(candidate_ids), nuevas


def main():
    print(f"\n{'='*70}")
    print(f"Org FCD Sweep — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"TEST_MODE: {'SI (solo lectura)' if TEST_MODE else 'NO — aplicando cambios'}")
    print(f"{'='*70}")

    checked_1, obsoletas = revalidate_existing_corrections()
    checked_2, nuevas = find_new_corrections()

    total = ch_get("SELECT count() AS n FROM bpa.org_fcd_corrections")[0]["n"]

    print(f"\n{'='*70}")
    print("Resumen")
    print(f"{'='*70}")
    print(f"Parte 1 — revisadas: {checked_1} | obsoletas borradas: {len(obsoletas)}")
    print(f"Parte 2 — revisadas: {checked_2} | nuevas insertadas: {len(nuevas)}")
    print(f"Total acumulado en bpa.org_fcd_corrections: {total}")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
