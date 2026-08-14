#!/usr/bin/env python3
"""
Todos los dias a las 7am Colombia, para los leads activos (no archivados)
cuyo owner sea Angie Rozo: si el lead NO tiene ninguna actividad futura
agendada (due_date >= hoy, sin importar el tipo ni que tan lejos este),
se le crea una actividad de tipo "Outbound Connection N" para que no se
quede sin proximo paso. La actividad se asocia al lead, a la persona del
lead, y a la organizacion del lead si tiene una.

El numero N depende de que tan completos estan los "toques" ya marcados
por lead_touch_numbering_backfill.py (subject con prefijo "Toque N - "):
un toque N se considera completo solo si el lead tiene AL MENOS una
actividad de aircall Y una de whatsapp marcadas "Toque N -". Se busca el
primer N (empezando en 1) que no este completo todavia; ese es el N de la
nueva actividad. Si los toques 1, 2 y 3 ya estan completos, no se crea
nada (no existe un tipo "Outbound Connection 4").

Alcance: por ahora solo Angie Rozo (owner_id 22793685). Si se quiere
extender a mas BDRs, agregar sus ids a OWNER_IDS.

TEST_MODE=true -> solo calcula y muestra que crearia, no escribe nada.
Cron: todos los dias 7am Colombia (UTC-5 -> 12:00 UTC).
"""

import os
import re
import json
import time
import requests
from datetime import date

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

OWNER_IDS = [22793685]  # Angie Rozo

OUTBOUND_TYPE_BY_TOUCH = {1: "outbound_connection_1", 2: "outbound_connection_2", 3: "outbound_connection_3"}
MAX_TOUCH = 3

AIRCALL_PREFIX = "aircall_"
TOQUE_RE = re.compile(r"^Toque (\d+) - ")

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


def api_post(endpoint, data):
    rate_limit()
    r = requests.post(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_active_leads_for_owners():
    leads = []
    for owner_id in OWNER_IDS:
        start = 0
        while True:
            resp = api_get("leads", {"owner_id": owner_id, "start": start, "limit": 500})
            data = resp.get("data") or []
            leads.extend(data)
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                start = pagination["next_start"]
            else:
                break
    return [l for l in leads if l.get("person_id")]


def get_person_activities(person_id):
    items, start = [], 0
    while True:
        resp = api_get(f"persons/{person_id}/activities", {"start": start, "limit": 500})
        data = resp.get("data") or []
        items.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return items


def has_future_activity(lead_activities, today_str):
    for a in lead_activities:
        if a.get("done"):
            continue
        due = a.get("due_date")
        if due and due >= today_str:
            return True
    return False


def next_touch_target(lead_activities):
    aircall_touches, whatsapp_touches = set(), set()
    for a in lead_activities:
        subject = a.get("subject") or ""
        m = TOQUE_RE.match(subject)
        if not m:
            continue
        n = int(m.group(1))
        t = a.get("type") or ""
        if t.startswith(AIRCALL_PREFIX):
            aircall_touches.add(n)
        elif t == "whatsapp":
            whatsapp_touches.add(n)

    for n in range(1, MAX_TOUCH + 1):
        if n in aircall_touches and n in whatsapp_touches:
            continue
        return n
    return None  # 1, 2 y 3 ya completos


def main():
    today_str = date.today().isoformat()

    print(f"\n{'='*60}")
    print(f"Outbound Connection Gap Filler — {today_str}")
    if TEST_MODE:
        print(f"MODO TEST: solo se procesaran los primeros {MAX_LEADS_TEST_MODE} leads")
    print(f"{'='*60}\n")

    leads = get_active_leads_for_owners()
    print(f"Leads activos de los owners configurados: {len(leads)}")

    if TEST_MODE:
        leads = leads[:MAX_LEADS_TEST_MODE]

    stats = {"created": 0, "skipped_future_activity": 0, "skipped_all_touches_complete": 0, "error": 0}
    result_log = []
    activities_cache = {}

    for i, lead in enumerate(leads, 1):
        lead_id = lead["id"]
        pid = lead["person_id"]
        title = lead.get("title", "?")

        if pid not in activities_cache:
            activities_cache[pid] = get_person_activities(pid)
        acts = activities_cache[pid]
        lead_activities = [a for a in acts if a.get("lead_id") == lead_id]

        if has_future_activity(lead_activities, today_str):
            stats["skipped_future_activity"] += 1
            continue

        target = next_touch_target(lead_activities)
        if target is None:
            print(f"[{i}/{len(leads)}] '{title}': toques 1-3 ya completos, no se crea nada.")
            stats["skipped_all_touches_complete"] += 1
            continue

        activity_type = OUTBOUND_TYPE_BY_TOUCH[target]
        subject = f"Outbound Connection {target}"

        if TEST_MODE:
            print(f"[{i}/{len(leads)}] '{title}': [TEST] crearia '{subject}' (type={activity_type}, due={today_str}, "
                  f"person_id={pid}, org_id={lead.get('organization_id')})")
            stats["created"] += 1
            result_log.append({"lead_id": lead_id, "subject": subject, "type": activity_type, "due_date": today_str,
                                "person_id": pid, "org_id": lead.get("organization_id")})
        else:
            payload = {
                "subject": subject,
                "type": activity_type,
                "due_date": today_str,
                "lead_id": lead_id,
                "person_id": pid,
                "user_id": OWNER_IDS[0] if len(OWNER_IDS) == 1 else lead.get("owner_id"),
                "done": 0,
            }
            if lead.get("organization_id"):
                payload["org_id"] = lead["organization_id"]
            try:
                resp = api_post("activities", payload)
                if resp.get("success"):
                    act_id = resp["data"]["id"]
                    print(f"[{i}/{len(leads)}] '{title}': actividad {act_id} creada -> '{subject}'")
                    stats["created"] += 1
                    result_log.append({"lead_id": lead_id, "activity_id": act_id, "subject": subject, "type": activity_type})
                else:
                    print(f"[{i}/{len(leads)}] '{title}': ERROR creando actividad: {resp}")
                    stats["error"] += 1
            except Exception as e:
                print(f"[{i}/{len(leads)}] '{title}': ERROR: {e}")
                stats["error"] += 1

    with open("outbound_connection_gap_filler_result_log.json", "w") as f:
        json.dump(result_log, f)

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['created']} creadas, {stats['skipped_future_activity']} ya tenian actividad futura, "
          f"{stats['skipped_all_touches_complete']} con toques 1-3 completos, {stats['error']} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
