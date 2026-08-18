#!/usr/bin/env python3
"""
Todos los dias a las 7am Colombia: para actividades asociadas a un lead
ACTIVO (no archivado), de cualquier owner, que llevan 2 o mas dias vencidas
(due_date <= hoy - 2 dias) y siguen sin marcarse como hechas, se les pone
Priority = Medium.

Ejemplo: una tarea con due_date = hoy (martes), si sigue sin hacerse para
la revision del jueves 7am (2 dias despues), pasa a Medium en esa corrida.

Solo se toca si la actividad no tiene ya una prioridad asignada (priority
es None) -- si alguien ya la marco manualmente (Medium o High), no se
sobreescribe. No hay escalamiento a High por ahora, solo este paso a
Medium.

TEST_MODE=true -> solo calcula y muestra que cambiaria, no escribe nada.
Cron: todos los dias 7am Colombia (UTC-5 -> 12:00 UTC).
"""

import os
import json
import time
import requests
from datetime import date

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

OVERDUE_DAYS_THRESHOLD = 2
PRIORITY_MEDIUM = 25

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


def api_put(endpoint, data):
    rate_limit()
    r = requests.put(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_active_leads():
    leads, start = [], 0
    while True:
        resp = api_get("leads", {"start": start, "limit": 500})
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


def days_overdue(due_date_str, today):
    try:
        due = date.fromisoformat(due_date_str[:10])
    except Exception:
        return None
    return (today - due).days


def main():
    today = date.today()

    print(f"\n{'='*60}")
    print(f"Overdue Activity Priority — {today.isoformat()}")
    if TEST_MODE:
        print(f"MODO TEST: solo se procesaran los primeros {MAX_LEADS_TEST_MODE} leads")
    print(f"{'='*60}\n")

    leads = get_active_leads()
    print(f"Leads activos: {len(leads)}")

    if TEST_MODE:
        leads = leads[:MAX_LEADS_TEST_MODE]

    stats = {"updated": 0, "already_has_priority": 0, "not_overdue_enough": 0, "error": 0}
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

        for a in lead_activities:
            if a.get("done"):
                continue
            if a.get("priority"):
                stats["already_has_priority"] += 1
                continue
            due = a.get("due_date")
            if not due:
                continue
            overdue = days_overdue(due, today)
            if overdue is None or overdue < OVERDUE_DAYS_THRESHOLD:
                stats["not_overdue_enough"] += 1
                continue

            if TEST_MODE:
                print(f"[{i}/{len(leads)}] '{title}' actividad {a['id']} (due={due}, {overdue}d vencida): [TEST] pondria Priority=Medium")
                stats["updated"] += 1
                result_log.append({"lead_id": lead_id, "activity_id": a["id"], "due_date": due, "days_overdue": overdue})
            else:
                try:
                    resp = api_put(f"activities/{a['id']}", {"priority": PRIORITY_MEDIUM})
                    if resp.get("success"):
                        print(f"[{i}/{len(leads)}] '{title}' actividad {a['id']} (due={due}, {overdue}d vencida): Priority=Medium")
                        stats["updated"] += 1
                        result_log.append({"lead_id": lead_id, "activity_id": a["id"], "due_date": due, "days_overdue": overdue})
                    else:
                        print(f"[{i}/{len(leads)}] '{title}' actividad {a['id']}: ERROR: {resp}")
                        stats["error"] += 1
                except Exception as e:
                    print(f"[{i}/{len(leads)}] '{title}' actividad {a['id']}: ERROR: {e}")
                    stats["error"] += 1

    with open("overdue_activity_priority_result_log.json", "w") as f:
        json.dump(result_log, f)

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['updated']} actualizadas a Medium, {stats['already_has_priority']} ya tenian prioridad, "
          f"{stats['not_overdue_enough']} aun no llegan a {OVERDUE_DAYS_THRESHOLD} dias vencidas, {stats['error']} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
