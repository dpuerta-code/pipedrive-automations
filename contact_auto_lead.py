#!/usr/bin/env python3
"""
Crea automaticamente un Lead en Pipedrive cuando se detecta contacto real
(WhatsApp, Aircall en cualquier variante, o email realmente enviado) con
una organizacion del programa de prospeccion "Prospección Claude".

No depende de ClickHouse (GitHub Actions no tiene acceso a esa red interna):
lee la lista de organizaciones a vigilar del tab "Roster" de la hoja de cola
(actualizado manualmente desde una sesion con acceso a ClickHouse cada vez
que bpa.sheet_prospeccion_matches cambia), y detecta contacto real solo con
la API de Pipedrive, con la misma logica que org_contacted_sync.py.

No toca el campo "Campaing" — eso es exclusivo del click en Metabase
(ver lead_queue_processor.py).

TEST_MODE=true -> solo imprime lo que haria, no crea nada.
Cron GitHub Actions: varias veces al dia.
"""

import os
import time
from datetime import date, timedelta

import gspread
import json
import requests

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

QUEUE_SPREADSHEET_ID = "1g2MVl8H17gTtmSMKPZKypPIXRwRCIboMYZycFLH9VnY"
ROSTER_TAB = "Roster"

LEAD_MARKER = "Prospección Claude"

CONTACT_ACTIVITY_TYPES = [
    "whatsapp",
    "aircall_outbound_answered_",
    "aircall_outbound_unanswere",
    "aircall_inbound_answered_c",
    "aircall_missed_call_with_v",
    "aircall_missed_call_withou",
    "aircall_inbound_whatsapp_m",
    "aircall_outbound_whatsapp_",
]

LOOKBACK_DAYS = 3  # mismo criterio de ventana que org_contacted_sync.py

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

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


def api_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": API_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{BASE_URL}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(endpoint, data):
    rate_limit()
    r = requests.post(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_roster():
    creds = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(QUEUE_SPREADSHEET_ID)
    ws = sh.worksheet(ROSTER_TAB)
    rows = ws.get_all_records()
    return [r for r in rows if str(r.get("org_id", "")).strip()]


def fetch_recent_activity_org_ids(person_cache):
    """org_id -> True si tuvo actividad whatsapp/aircall en la ventana."""
    start_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = date.today().isoformat()
    org_ids = set()
    for t in CONTACT_ACTIVITY_TYPES:
        start = 0
        while True:
            resp = api_get("activities", {
                "type": t, "user_id": 0, "limit": 500, "start": start,
                "start_date": start_date, "end_date": end_date,
            })
            data = resp.get("data") or []
            for a in data:
                org_id = a.get("org_id")
                if not org_id:
                    person_id = a.get("person_id")
                    if person_id:
                        if person_id not in person_cache:
                            try:
                                pr = api_get(f"persons/{person_id}")
                                person_cache[person_id] = pr.get("data") or {}
                            except Exception:
                                person_cache[person_id] = {}
                        org = person_cache[person_id].get("org_id")
                        org_id = org.get("value") if isinstance(org, dict) else org
                if org_id:
                    org_ids.add(int(org_id))
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                start = pagination["next_start"]
            else:
                break
    return org_ids


def fetch_recent_email_org_ids():
    """org_id -> True si alguna persona de esa org tiene last_outgoing_mail_time reciente."""
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    org_ids = set()
    start = 0
    while True:
        resp = api_get("persons", {"limit": 500, "start": start})
        data = resp.get("data") or []
        for p in data:
            mail_time = p.get("last_outgoing_mail_time")
            org = p.get("org_id")
            org_id = org.get("value") if isinstance(org, dict) else org
            if mail_time and org_id:
                try:
                    d = date(*map(int, mail_time[:10].split("-")))
                except Exception:
                    continue
                if d >= cutoff:
                    org_ids.add(int(org_id))
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return org_ids


def existing_program_lead(org_id):
    resp = api_get("leads", {"organization_id": org_id, "limit": 100})
    for lead in resp.get("data") or []:
        if LEAD_MARKER in (lead.get("title") or ""):
            return lead
    return None


def find_person_by_email(email):
    if not email:
        return None
    resp = api_get("persons/search", {"term": email, "fields": "email", "exact_match": "true"})
    items = (resp.get("data") or {}).get("items") or []
    if items:
        return items[0]["item"]["id"]
    return None


def create_person(nombre, email, org_id):
    body = {"name": nombre or email, "org_id": org_id}
    if email:
        body["email"] = [{"value": email, "primary": True}]
    resp = api_post("persons", body)
    return resp["data"]["id"]


def create_lead(sheet_company, org_id, person_id=None):
    body = {
        "title": f"{sheet_company} - {LEAD_MARKER}",
        "organization_id": org_id,
    }
    if person_id:
        body["person_id"] = person_id
    resp = api_post("leads", body)
    return resp["data"]["id"]


def main():
    print(f"\n{'='*60}")
    print("Contact Auto Lead")
    if TEST_MODE:
        print("MODO TEST: solo lectura, no se crea nada en Pipedrive")
    print(f"{'='*60}\n")

    roster = get_roster()
    print(f"Roster: {len(roster)} organizaciones a vigilar.")

    person_cache = {}
    activity_org_ids = fetch_recent_activity_org_ids(person_cache)
    email_org_ids = fetch_recent_email_org_ids()
    contacted_org_ids = activity_org_ids | email_org_ids
    print(f"Orgs con contacto real en los ultimos {LOOKBACK_DAYS} dias (whatsapp/aircall/email): {len(contacted_org_ids)}")

    created = 0
    skipped_existing = 0
    skipped_no_contact = 0
    errors = 0

    for row in roster:
        org_id = int(row["org_id"])
        sheet_company = row.get("sheet_company", "")
        org_category = row.get("org_category", "Coverage")
        contact_nombre = row.get("contact_nombre", "")
        email = row.get("email", "")

        if org_id not in contacted_org_ids:
            skipped_no_contact += 1
            continue

        try:
            if existing_program_lead(org_id):
                skipped_existing += 1
                continue

            if TEST_MODE:
                if org_category == "Coverage":
                    print(f"  [TEST] '{sheet_company}' (org {org_id}, Coverage): buscaria/crearia Persona '{contact_nombre}' <{email}>, luego crearia Lead.")
                else:
                    print(f"  [TEST] '{sheet_company}' (org {org_id}, {org_category}): crearia Lead ligado solo a organization_id.")
                created += 1
                continue

            person_id = None
            if org_category == "Coverage":
                person_id = find_person_by_email(email)
                if not person_id:
                    person_id = create_person(contact_nombre, email, org_id)

            lead_id = create_lead(sheet_company, org_id, person_id)
            print(f"  '{sheet_company}' (org {org_id}): Lead creado (id {lead_id}) por contacto real detectado.")
            created += 1
        except Exception as e:
            print(f"  ERROR en org {org_id} ('{sheet_company}'): {e}")
            errors += 1

    print(f"\n{'='*60}")
    print(f"Resumen: {created} leads creados, {skipped_existing} ya tenian lead del programa, "
          f"{skipped_no_contact} sin contacto reciente, {errors} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
