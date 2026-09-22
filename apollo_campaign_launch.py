#!/usr/bin/env python3
"""
Enrola en una secuencia de Apollo todos los Leads de Pipedrive con
Organization Category = Coverage o New (ya no se exige Campaing=Yes; ver
memoria/PR del 2026-09-22 -- el campo Campaing se deja de usar como gate).

La secuencia destino depende de "Organization Category" + "Source channel" del lead:
  - Source channel = Org Scoring:
      Organization Category = New      -> Motor BDR - SL·NEW
      Organization Category = Coverage -> Motor BDR - SL·REACT
      (Comeback / Marketing -> sin secuencia, se saltan)
  - Cualquier otro Source channel:
      Organization Category = Coverage -> Coverage {País} (MX/CL/Colombia, o LATAM si el país
                                            no tiene secuencia propia)
      (Comeback / New / Marketing -> sin secuencia, se saltan)

Ademas, se salta cualquier lead cuya organizacion tenga el campo "Logo Type" = Ironman,
Champion o Target MX Champion (ids 313/314/1418), sin importar la categoria/canal.

Se salta cualquier lead creado ANTES de LEAD_CREATED_SINCE (ver constante abajo) -- al
quitar el gate de Campaing=Yes, el pool de leads elegibles crecio de golpe (~17-19/dia a
~370+ en un solo run); este filtro de fecha evita re-contactar leads viejos que ya podrian
tener historial de correos previo, limitando el primer alcance a leads recien creados.

Las secuencias Motor BDR (SL·NEW y SL·REACT) usan variables de personalizacion por contacto
({{contact.Slang Ciudad}}, {{contact.Slang Senal}}, etc, ver plantillas en Apollo) que vienen
de la hoja "Prospección Slang" (Google Sheets). Ese contenido se busca por email en
prospeccion_signals.csv (snapshot local, ver nota en el propio archivo); si un lead va para
Motor BDR y no aparece ahi, se salta -- mejor no enrolar que mandar un correo con la variable
vacia o el texto literal "{{contact.Slang X}}". Las 4 secuencias Coverage no lo necesitan,
solo usan {{first_name}}/{{company}}/{{account.name}}, que siempre se llenan.

No se guardan campos nuevos en Pipedrive (contact_id/sequence_id/fecha de envío). El cruce
Apollo<->Pipedrive es siempre por email. La idempotencia la maneja Apollo: POST /contacts con
run_dedupe=true devuelve el contacto existente si el email ya está en Apollo, y
add_contact_ids salta (sin error) contactos que ya están activos en esa secuencia.

TEST_MODE=true  -> solo lectura, no llama a Apollo (default).
Cron GitHub Actions: lunes a viernes.
"""

import csv
import json
import os
import time
import requests
from datetime import datetime, timezone

PIPEDRIVE_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
PIPEDRIVE_BASE = "https://slang.pipedrive.com/api/v1"
APOLLO_API_KEY = os.environ["APOLLO_API_KEY"]
APOLLO_BASE = "https://api.apollo.io/api/v1"

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

# Filtro Pipedrive "Org Category Coverage/New (Apollo automation)" - creado 2026-09-22.
# Reemplaza al filtro anterior "Campaing Yes" (id 71499, ya no se usa como gate de entrada).
ORG_CATEGORY_FILTER_ID = 71609

# Solo se enrolan leads creados en/despues de esta fecha (YYYY-MM-DD, se compara contra
# add_time del lead). Evita re-contactar leads viejos ahora que no hay gate de Campaing=Yes.
LEAD_CREATED_SINCE = os.environ.get("LEAD_CREATED_SINCE", "2026-09-18")

ORG_CAT_KEY = "fafdb80a27427f23c8f72675767e616461f056cb"
CHANNEL_KEY = "channel"  # campo nativo "Source channel"

# Org field "Logo Type" (ver memoria project_pipedrive_logo_type_champion_ironman).
# OJO: Pipedrive devuelve el valor de este campo como STRING ("313"), no como int -- comparar
# siempre como string (mismo bug ya documentado en esa memoria para el campo Country).
ORG_LOGO_TYPE_KEY = "b5a5290a565880a732f018c364e182719520bdd2"
LOGO_TYPE_EXCLUDED = {"313", "314", "1418"}  # Ironman, Champion, Target MX Champion

ORG_SCORING_CHANNEL = 1431
COMEBACK = 1215
COVERAGE = 1216
NEW = 1217
MARKETING = 1218

SEQ_NEW = "6a53f346a203ac0013560c86"  # Motor BDR - SL·NEW
SEQ_REACT = "6a53f362a203ac000ce374f3"  # Motor BDR - SL·REACT
SEQ_COVERAGE_MX = "69cd6e73276e4f00111247a4"
SEQ_COVERAGE_CL = "69d518168f9bb3002149790d"
SEQ_COVERAGE_COLOMBIA = "69d3ae9434d0330021a63fbc"
SEQ_COVERAGE_LATAM = "69d6ae5d10d96f0011bc571b"

COUNTRY_SEQ = {
    "Mexico": SEQ_COVERAGE_MX,
    "Chile": SEQ_COVERAGE_CL,
    "Colombia": SEQ_COVERAGE_COLOMBIA,
}

# Mailbox de envío por defecto (a.rozo@slangapp.com, default:true en /email_accounts)
SEND_FROM_EMAIL_ACCOUNT_ID = "674e10be0c6c6d02cf03f630"

# Secuencias que requieren las variables de personalizacion "Slang X" (ver mas abajo).
# Las 4 Coverage NO las usan (solo {{first_name}}/{{company}}/{{account.name}}), no necesitan lookup.
SEQUENCES_REQUIRING_SIGNALS = {SEQ_NEW, SEQ_REACT}

# Campos custom de contacto en Apollo usados como merge tags {{contact.Slang X}} en las
# plantillas de Motor BDR. IDs obtenidos via apollo_fields_index (modality=contact).
SLANG_FIELD_IDS = {
    "slang_ciudad": "6a53ec1522bf8b0014fde5e8",
    "slang_senal": "6a53ec15af729a0010cdf4e8",
    "slang_dolor_industria": "6a53ec14c34eb30018172115",
    "slang_rol_hook": "6a53ec149e3388000c9585cc",
    "slang_insight": "6a53ec14e38b03001cac77ed",
    "slang_caso_exito": "6a53ec1422bf8b0014fde5e6",
}

# Snapshot de la hoja "Prospección Slang" (Sheet1 + Prospección T2), exportado a mano el
# 2026-09-09 porque las herramientas de Drive disponibles solo leen la pestaña por defecto.
# TODO: reemplazar por una lectura en vivo de la hoja (todas las pestañas) cuando haya una
# forma de acceder a pestañas especificas -- este CSV se desactualiza con cada tanda nueva.
PROSPECCION_SIGNALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prospeccion_signals.csv")


def load_prospeccion_signals():
    signals = {}
    if not os.path.exists(PROSPECCION_SIGNALS_PATH):
        return signals
    with open(PROSPECCION_SIGNALS_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            signals[row["email"]] = row
    return signals


request_count = 0
window_start = time.time()


def rate_limit():
    global request_count, window_start
    request_count += 1
    if request_count >= 80:
        elapsed = time.time() - window_start
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        request_count = 0
        window_start = time.time()


def pd_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": PIPEDRIVE_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{PIPEDRIVE_BASE}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def apollo_post(endpoint, json_body):
    rate_limit()
    headers = {"x-api-key": APOLLO_API_KEY, "Content-Type": "application/json"}
    r = requests.post(f"{APOLLO_BASE}/{endpoint}", json=json_body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def apollo_get(endpoint, params=None):
    rate_limit()
    headers = {"x-api-key": APOLLO_API_KEY}
    r = requests.get(f"{APOLLO_BASE}/{endpoint}", params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def country_from_title(title):
    """Heurística por sufijo de nombre (más confiable que el campo Country, ver memoria
    project_pipedrive_logo_type_champion_ironman: casos donde Country venía mal para la org)."""
    t = (title or "").lower()
    if t.strip().endswith(" mx") or " mx " in f" {t} ":
        return "Mexico"
    mapping = {"mexico": "Mexico", "méxico": "Mexico", "colombia": "Colombia", "chile": "Chile"}
    for k, v in mapping.items():
        if k in t:
            return v
    return None


def resolve_sequence(org_category, channel, title):
    """Devuelve (sequence_id, skip_reason). Si skip_reason no es None, no se enrola."""
    if org_category == COMEBACK:
        return None, "comeback_sin_secuencia"
    if org_category == MARKETING:
        return None, "marketing_sin_mapeo"

    is_org_scoring = channel == ORG_SCORING_CHANNEL

    if is_org_scoring:
        if org_category == NEW:
            return SEQ_NEW, None
        if org_category == COVERAGE:
            return SEQ_REACT, None
        return None, "combinacion_sin_mapeo"

    if org_category in (COVERAGE, NEW):
        country = country_from_title(title)
        return COUNTRY_SEQ.get(country, SEQ_COVERAGE_LATAM), None
    return None, "combinacion_sin_mapeo"


def get_eligible_leads():
    leads = []
    start = 0
    while True:
        resp = pd_get("leads", {"filter_id": ORG_CATEGORY_FILTER_ID, "limit": 500, "start": start})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return leads


def get_person(person_id):
    resp = pd_get(f"persons/{person_id}")
    return resp.get("data") or {}


def get_organization(org_id):
    resp = pd_get(f"organizations/{org_id}")
    return resp.get("data") or {}


_pipedrive_user_email_cache = {}


def get_pipedrive_user_email(user_id):
    if user_id in _pipedrive_user_email_cache:
        return _pipedrive_user_email_cache[user_id]
    email = None
    try:
        resp = pd_get(f"users/{user_id}")
        email = (resp.get("data") or {}).get("email")
    except Exception:
        pass
    _pipedrive_user_email_cache[user_id] = email
    return email


def get_apollo_mailboxes():
    """{email: account_id} de las mailboxes conectadas en Apollo."""
    resp = apollo_get("email_accounts")
    return {a["email"]: a["id"] for a in resp.get("email_accounts", []) if a.get("active")}


def resolve_sender_account_id(owner_id, apollo_mailboxes):
    """
    Manda desde el mailbox Apollo del owner del lead en Pipedrive. Los owners usan
    @slangapp.com en Pipedrive; en Apollo los mailboxes de envio suelen estar en
    @slangprogram.com con el mismo local-part (mismo patron para todos menos a.rozo,
    que ademas tiene un mailbox exacto en @slangapp.com). Si no hay match de ningun tipo,
    cae al mailbox default.
    """
    owner_email = get_pipedrive_user_email(owner_id) if owner_id else None
    if owner_email:
        if owner_email in apollo_mailboxes:
            return apollo_mailboxes[owner_email], owner_email
        local_part = owner_email.split("@", 1)[0]
        alt_email = f"{local_part}@slangprogram.com"
        if alt_email in apollo_mailboxes:
            return apollo_mailboxes[alt_email], alt_email
    return SEND_FROM_EMAIL_ACCOUNT_ID, None


def extract_id(value):
    if isinstance(value, dict):
        return value.get("value")
    return value


def primary_email(person):
    emails = person.get("email") or []
    for e in emails:
        if e.get("primary"):
            return e.get("value")
    if emails:
        return emails[0].get("value")
    return None


def find_or_create_apollo_contact(email, full_name, org_name, typed_custom_fields=None):
    parts = (full_name or "").strip().split(" ", 1)
    first_name = parts[0] if parts and parts[0] else "N/D"
    last_name = parts[1] if len(parts) > 1 else "N/D"
    body = {
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "organization_name": org_name,
        "run_dedupe": True,
    }
    if typed_custom_fields:
        body["typed_custom_fields"] = typed_custom_fields
    resp = apollo_post("contacts", body)
    contact = resp.get("contact") or {}
    return contact.get("id")


def add_to_sequence(contact_id, sequence_id, send_from_account_id):
    body = {
        "emailer_campaign_id": sequence_id,
        "contact_ids": [contact_id],
        "send_email_from_email_account_id": send_from_account_id,
    }
    return apollo_post(f"emailer_campaigns/{sequence_id}/add_contact_ids", body)


def main():
    print(f"\n{'='*60}")
    print(f"Apollo Campaign Launch -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se llamara a Apollo (solo lectura)")
    print(f"{'='*60}\n")

    leads = get_eligible_leads()
    print(f"Leads con Organization Category Coverage/New: {len(leads)}\n")

    signals = load_prospeccion_signals()
    print(f"Señales de personalización cargadas (prospeccion_signals.csv): {len(signals)}\n")

    try:
        apollo_mailboxes = get_apollo_mailboxes()
        print(f"Mailboxes Apollo conectadas: {len(apollo_mailboxes)}\n")
    except Exception as e:
        print(f"ADVERTENCIA: no se pudieron listar mailboxes Apollo ({e}); se usara el default para todos\n")
        apollo_mailboxes = {}

    seen_emails = set()
    stats = {"enrolled": 0, "already_active": 0, "no_sequence": 0, "logo_type_excluded": 0,
             "too_old": 0, "no_signals": 0, "no_email": 0, "duplicate": 0, "errors": 0}
    backup = []

    for lead in leads:
        title = lead.get("title", "?")
        person_id = extract_id(lead.get("person_id"))
        org_id = extract_id(lead.get("organization_id"))
        owner_id = extract_id(lead.get("owner_id"))
        org_category = lead.get(ORG_CAT_KEY)
        channel = lead.get(CHANNEL_KEY)
        add_time = lead.get("add_time") or ""

        if add_time[:10] < LEAD_CREATED_SINCE:
            stats["too_old"] += 1
            continue

        sequence_id, skip_reason = resolve_sequence(org_category, channel, title)
        if skip_reason:
            stats["no_sequence"] += 1
            continue

        if org_id:
            try:
                org = get_organization(org_id)
                logo_type = org.get(ORG_LOGO_TYPE_KEY)
                if str(logo_type) in LOGO_TYPE_EXCLUDED:
                    print(f"  SKIP '{title}': org Logo Type excluido ({logo_type})")
                    stats["logo_type_excluded"] += 1
                    continue
            except Exception as e:
                print(f"  ERROR obteniendo organization {org_id} ('{title}'): {e}")
                stats["errors"] += 1
                continue

        if not person_id:
            print(f"  SKIP '{title}': sin person_id")
            stats["errors"] += 1
            continue

        try:
            person = get_person(person_id)
        except Exception as e:
            print(f"  ERROR obteniendo person {person_id} ('{title}'): {e}")
            stats["errors"] += 1
            continue

        email = primary_email(person)
        if not email or "@" not in email:
            print(f"  SKIP '{title}': sin email valido")
            stats["no_email"] += 1
            continue

        if email in seen_emails:
            print(f"  SKIP '{title}' ({email}): duplicado en este run")
            stats["duplicate"] += 1
            continue
        seen_emails.add(email)

        typed_custom_fields = None
        if sequence_id in SEQUENCES_REQUIRING_SIGNALS:
            row = signals.get(email)
            if not row or not row.get("slang_senal"):
                print(f"  SKIP '{title}' ({email}): sin señales de personalización en la hoja "
                      f"(requeridas para Motor BDR, se saltaria el correo roto)")
                stats["no_signals"] += 1
                continue
            typed_custom_fields = {SLANG_FIELD_IDS[k]: row[k] for k in SLANG_FIELD_IDS if row.get(k)}

        org_name = title  # el titulo del lead ya suele llevar el nombre de la org
        full_name = person.get("name", "")
        send_from_account_id, send_from_email = resolve_sender_account_id(owner_id, apollo_mailboxes)

        if TEST_MODE:
            sender_note = f" desde {send_from_email}" if send_from_email else " desde default (sin mailbox del owner)"
            print(f"  [TEST] Enrolaria '{full_name}' <{email}> ({title}) -> secuencia {sequence_id}"
                  f"{' (con senales)' if typed_custom_fields else ''}{sender_note}")
            stats["enrolled"] += 1
            backup.append({"lead": title, "email": email, "sequence_id": sequence_id})
            continue

        try:
            contact_id = find_or_create_apollo_contact(email, full_name, org_name, typed_custom_fields)
            if not contact_id:
                print(f"  ERROR: no se pudo resolver contact_id para {email}")
                stats["errors"] += 1
                continue
            resp = add_to_sequence(contact_id, sequence_id, send_from_account_id)
            skipped = resp.get("skipped_contact_ids") or {}
            if contact_id in skipped:
                print(f"  SKIP '{full_name}' <{email}>: ya activo en la secuencia ({skipped[contact_id]})")
                stats["already_active"] += 1
            else:
                print(f"  OK '{full_name}' <{email}> -> {sequence_id}")
                stats["enrolled"] += 1
                backup.append({"lead": title, "email": email, "contact_id": contact_id, "sequence_id": sequence_id})
        except Exception as e:
            print(f"  ERROR enrolando {email}: {e}")
            stats["errors"] += 1

    if backup:
        with open("apollo_campaign_launch_backup.json", "w") as f:
            json.dump(backup, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Resumen: {json.dumps(stats, ensure_ascii=False)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
