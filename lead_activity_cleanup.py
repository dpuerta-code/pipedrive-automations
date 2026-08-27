#!/usr/bin/env python3
"""
lead_activity_cleanup.py

Busca leads archivados recientemente (últimos LOOKBACK_DAYS días) y elimina
todas sus actividades pendientes (done=0), excepto las de tipo "retomar".

Cubre el caso en que un lead queda archivado pero le sobran tareas to-do
que el script de archivación no alcanzó a limpiar.

TEST_MODE=true  → solo lectura, no elimina nada.
LOOKBACK_DAYS   → cuántos días atrás buscar leads archivados (default 7).
Cron: 1 vez al día.
"""

import os
import requests
import time
from datetime import datetime, timezone, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL  = "https://slang.pipedrive.com/api/v1"

TEST_MODE    = os.environ.get("TEST_MODE", "true").lower() == "true"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

request_count = 0
window_start  = time.time()


def rate_limit():
    global request_count, window_start
    request_count += 1
    if request_count >= 78:
        elapsed = time.time() - window_start
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        request_count = 0
        window_start  = time.time()


def api_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": API_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{BASE_URL}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def api_delete(endpoint):
    rate_limit()
    r = requests.delete(f"{BASE_URL}/{endpoint}",
                        params={"api_token": API_TOKEN}, timeout=30)
    r.raise_for_status()
    return r.json()


def get_recently_archived_leads(since_dt):
    """Devuelve leads archivados con archive_time >= since_dt."""
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"archived_status": "archived", "limit": 500, "start": start})
        data = resp.get("data") or []
        if not data:
            break
        for lead in data:
            archive_time = lead.get("archive_time") or ""
            if not archive_time:
                continue
            try:
                at = datetime.strptime(archive_time[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if at >= since_dt:
                leads.append(lead)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return leads


def get_pending_activities(lead_id):
    """Actividades to-do (done=0) de un lead."""
    activities = []
    start = 0
    while True:
        resp = api_get("activities", {
            "lead_id": lead_id,
            "done": 0,
            "limit": 500,
            "start": start,
        })
        data = resp.get("data") or []
        if not data:
            break
        activities.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return activities


def main():
    now_utc  = datetime.now(timezone.utc)
    since_dt = now_utc - timedelta(days=LOOKBACK_DAYS)

    print(f"\n{'='*60}")
    print(f"Lead Activity Cleanup — {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se eliminará nada")
    print(f"Buscando leads archivados desde {since_dt.strftime('%Y-%m-%d')} ({LOOKBACK_DAYS} días)")
    print(f"{'='*60}\n")

    leads = get_recently_archived_leads(since_dt)
    print(f"Leads archivados en ventana: {len(leads)}\n")

    if not leads:
        print("Nada que limpiar.")
        return

    total_deleted = 0
    total_skipped = 0
    total_errors  = 0

    for lead in leads:
        lead_id    = lead["id"]
        lead_title = lead.get("title", "?")
        archive_time = (lead.get("archive_time") or "")[:10]

        activities = get_pending_activities(lead_id)
        if not activities:
            continue

        print(f"Lead '{lead_title}' ({lead_id}) — archivado {archive_time} — {len(activities)} tarea(s) pendiente(s)")

        for act in activities:
            act_id      = act["id"]
            act_subject = act.get("subject", "?")
            act_type    = act.get("type", "")

            if act_type == "retomar":
                print(f"  SKIP {act_id} '{act_subject}' (tipo retomar)")
                total_skipped += 1
                continue

            if TEST_MODE:
                print(f"  [TEST] Eliminaría {act_id} '{act_subject}' (tipo {act_type})")
                total_deleted += 1
            else:
                try:
                    api_delete(f"activities/{act_id}")
                    print(f"  Eliminada {act_id} '{act_subject}' (tipo {act_type})")
                    total_deleted += 1
                except Exception as e:
                    print(f"  ERROR eliminando {act_id}: {e}")
                    total_errors += 1

    print(f"\n{'='*60}")
    if TEST_MODE:
        print(f"[TEST] Habría eliminado {total_deleted}, saltado {total_skipped} retomar, {total_errors} errores")
    else:
        print(f"Resumen: {total_deleted} eliminadas, {total_skipped} retomar conservadas, {total_errors} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
