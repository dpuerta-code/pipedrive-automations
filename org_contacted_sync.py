#!/usr/bin/env python3
"""
Marca "Org First Contact Date" en cada lead activo cuando detecta el primer
contacto real con esa organización.

Contacto = actividad de WhatsApp, cualquier llamada de Aircall, o un correo
realmente enviado (last_outgoing_mail_time de la persona).

Regla: solo se llena el campo si el contacto ocurre a menos de 60 días
(antes o después) del "Prospection Date" propio de ese lead. Una vez
lleno, nunca se vuelve a tocar (write-once).

TEST_MODE=true → solo procesa hasta MAX_LEADS_TEST_MODE leads pendientes
(para verificar antes de activar en masa)
Cron GitHub Actions: lunes-viernes 10am, 1:30pm y 6pm Colombia
"""

import os
import requests
import time
from datetime import datetime, date, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

PROSPECTION_DATE_KEY = "2db7aeb0017118ae0c5f9284887c0d55482bbce9"  # Prospection Date
CONTACT_DATE_KEY = "d81b6ab138f59f521821c5b29f1dc389b7a0cad4"      # Org First Contact Date

WINDOW_DAYS = 60      # +/- dias alrededor del Prospection Date que cuentan como "contacto valido"
LOOKBACK_DAYS = 3      # cuantos dias hacia atras de actividad se revisan en cada corrida

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

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
MAX_LEADS_TEST_MODE = 5

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


def api_patch(endpoint, data):
    rate_limit()
    r = requests.patch(
        f"{BASE_URL}/{endpoint}",
        params={"api_token": API_TOKEN},
        json=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def to_date(s):
    y, m, d = map(int, s[:10].split("-"))
    return date(y, m, d)


def get_pending_leads():
    """Todos los leads activos (no archivados) que aun no tienen Org First Contact Date."""
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"limit": 500, "start": start})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [l for l in leads if not l.get(CONTACT_DATE_KEY) and l.get(PROSPECTION_DATE_KEY) and l.get("organization_id")]


def get_org_id_from_activity(activity, person_cache):
    org = activity.get("org_id")
    if org:
        return org
    person_id = activity.get("person_id")
    if not person_id:
        return None
    if person_id not in person_cache:
        try:
            resp = api_get(f"persons/{person_id}")
            person_cache[person_id] = resp.get("data") or {}
        except Exception:
            person_cache[person_id] = {}
    person = person_cache[person_id]
    org = person.get("org_id")
    return org.get("value") if isinstance(org, dict) else org


def fetch_recent_activity_dates_by_org(person_cache):
    """org_id -> lista de fechas (str YYYY-MM-DD) de contacto reciente."""
    start_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = date.today().isoformat()
    by_org = {}
    for t in CONTACT_ACTIVITY_TYPES:
        start = 0
        while True:
            resp = api_get("activities", {
                "type": t, "user_id": 0, "limit": 500, "start": start,
                "start_date": start_date, "end_date": end_date,
            })
            data = resp.get("data") or []
            for a in data:
                org_id = get_org_id_from_activity(a, person_cache)
                if not org_id:
                    continue
                d = a.get("marked_as_done_time") or a.get("add_time")
                if d:
                    by_org.setdefault(org_id, []).append(d[:10])
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                start = pagination["next_start"]
            else:
                break
    return by_org


def find_qualifying_contact_date(lead, org_activity_dates, person_cache):
    prosp = to_date(lead[PROSPECTION_DATE_KEY])
    candidates = list(org_activity_dates.get(lead["organization_id"], []))

    person_id = lead.get("person_id")
    if person_id:
        if person_id not in person_cache:
            try:
                resp = api_get(f"persons/{person_id}")
                person_cache[person_id] = resp.get("data") or {}
            except Exception:
                person_cache[person_id] = {}
        mail_time = person_cache[person_id].get("last_outgoing_mail_time")
        if mail_time:
            candidates.append(mail_time[:10])

    qualifying = [c for c in candidates if abs((to_date(c) - prosp).days) <= WINDOW_DAYS]
    return min(qualifying) if qualifying else None


def main():
    print(f"\n{'='*60}")
    print(f"Org Contacted Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if TEST_MODE:
        print(f"MODO TEST: solo se procesaran hasta {MAX_LEADS_TEST_MODE} leads pendientes")
    print(f"{'='*60}\n")

    pending = get_pending_leads()
    print(f"Leads activos pendientes de Org First Contact Date: {len(pending)}")
    if not pending:
        print("Nada que hacer.")
        return

    if TEST_MODE:
        pending = pending[:MAX_LEADS_TEST_MODE]
        print(f"TEST MODE: procesando solo los primeros {len(pending)}.\n")

    print(f"Buscando actividad de contacto de los ultimos {LOOKBACK_DAYS} dias...")
    person_cache = {}
    org_activity_dates = fetch_recent_activity_dates_by_org(person_cache)
    print(f"Organizaciones con actividad reciente: {len(org_activity_dates)}\n")

    stats = {"updated": 0, "no_match": 0, "error": 0}

    for i, lead in enumerate(pending, 1):
        lead_id = lead["id"]
        title = lead.get("title", "?")
        contact_date = find_qualifying_contact_date(lead, org_activity_dates, person_cache)

        if not contact_date:
            stats["no_match"] += 1
            continue

        # re-chequeo justo antes de escribir, por seguridad write-once
        try:
            fresh = api_get(f"leads/{lead_id}")
            if fresh.get("data", {}).get(CONTACT_DATE_KEY):
                print(f"[{i}/{len(pending)}] '{title}': ya se lleno en paralelo, se omite.")
                continue
            resp = api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: contact_date})
            if resp.get("success"):
                print(f"[{i}/{len(pending)}] '{title}': Org First Contact Date = {contact_date}")
                stats["updated"] += 1
            else:
                stats["error"] += 1
        except Exception as e:
            print(f"[{i}/{len(pending)}] ERROR en lead {lead_id}: {e}")
            stats["error"] += 1

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['updated']} actualizados, {stats['no_match']} sin contacto en ventana, {stats['error']} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
