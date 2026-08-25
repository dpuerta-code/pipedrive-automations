#!/usr/bin/env python3
"""
Al final de cada día detecta deals cuyo campo "SAL date" sea igual a hoy.
Para cada organización afectada:
  1. Elimina las tareas (actividades) de todos sus leads activos.
  2. Archiva esos leads.

TEST_MODE=true  → muestra qué haría sin ejecutar cambios (solo lectura).
BACKFILL_MODE=true → procesa TODOS los deals open con SAL date lleno, sin filtro de fecha.
Cron GitHub Actions: todos los días 00:00 UTC = 7pm Colombia (UTC-5).
"""

import json
import os
import requests
import time
from datetime import datetime, timezone, timedelta, date

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

SAL_DATE_KEY = "8a4d1715b308943f49d7e5b270a7ea81d6f356b2"

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
# BACKFILL_MODE=true → ignora la fecha y procesa todos los deals open con SAL date lleno
BACKFILL_MODE = os.environ.get("BACKFILL_MODE", "false").lower() == "true"

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


def api_delete(endpoint):
    rate_limit()
    r = requests.delete(
        f"{BASE_URL}/{endpoint}",
        params={"api_token": API_TOKEN},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def get_deals_with_sal_date(today_str=None):
    """
    Pagina todos los deals open con SAL date.
    Si today_str es None (BACKFILL_MODE), devuelve todos los que tengan SAL date lleno.
    Si today_str esta definido, filtra solo los de hoy.
    """
    deals = []
    start = 0
    while True:
        resp = api_get("deals", {
            "status": "open",
            "limit": 500,
            "start": start,
        })
        data = resp.get("data") or []
        if not data:
            break
        for deal in data:
            sal_date = deal.get(SAL_DATE_KEY)
            if not sal_date:
                continue
            if today_str is None or str(sal_date)[:10] == today_str:
                deals.append(deal)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return deals


def extract_org_id(deal):
    """Extrae org_id del deal (puede venir como int o como dict {value, name})."""
    org = deal.get("org_id")
    if org is None:
        return None, "?"
    if isinstance(org, dict):
        return org.get("value"), org.get("name", "?")
    return org, "?"


def get_active_leads_for_org(org_id):
    """Leads no archivados de una organizacion."""
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {
            "organization_id": org_id,
            "archived_status": "not_archived",
            "limit": 500,
            "start": start,
        })
        data = resp.get("data") or []
        if not data:
            break
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return leads


def get_activities_for_lead(lead_id):
    """Actividades (tareas) vinculadas directamente a un lead."""
    activities = []
    start = 0
    while True:
        resp = api_get("activities", {
            "lead_id": lead_id,
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


def process_org(org_id, org_name):
    """Archiva leads activos de la org y elimina sus tareas. Devuelve stats."""
    leads = get_active_leads_for_org(org_id)
    if not leads:
        print(f"    Sin leads activos")
        return {"leads_archived": 0, "tasks_deleted": 0, "errors": 0}

    archived = 0
    deleted = 0
    errors = 0

    for lead in leads:
        lead_id = lead["id"]
        lead_title = lead.get("title", "?")

        try:
            activities = get_activities_for_lead(lead_id)
            if activities:
                print(f"    Lead '{lead_title}': {len(activities)} tarea(s)")
            for act in activities:
                act_id = act["id"]
                act_subject = act.get("subject", "?")
                act_type = act.get("type", "")
                if act_type == "retomar":
                    print(f"      SKIP tarea {act_id} '{act_subject}' (tipo retomar)")
                    continue
                if TEST_MODE:
                    print(f"      [TEST] Eliminaria tarea {act_id} '{act_subject}'")
                    deleted += 1
                else:
                    try:
                        api_delete(f"activities/{act_id}")
                        print(f"      Tarea {act_id} '{act_subject}' eliminada")
                        deleted += 1
                    except Exception as e:
                        print(f"      ERROR eliminando tarea {act_id}: {e}")
                        errors += 1
        except Exception as e:
            print(f"    ERROR obteniendo tareas del lead '{lead_title}': {e}")
            errors += 1

        if TEST_MODE:
            print(f"    [TEST] Archivaria lead '{lead_title}' ({lead_id})")
            archived += 1
        else:
            try:
                resp = api_patch(f"leads/{lead_id}", {"is_archived": True})
                if resp.get("success"):
                    print(f"    Lead '{lead_title}' ({lead_id}) -> archivado")
                    archived += 1
                else:
                    print(f"    Lead '{lead_title}' ({lead_id}) -> FAIL al archivar")
                    errors += 1
            except Exception as e:
                print(f"    ERROR archivando lead {lead_id}: {e}")
                errors += 1

    return {"leads_archived": archived, "tasks_deleted": deleted, "errors": errors}


def main():
    colombia_now = datetime.now(timezone.utc) - timedelta(hours=5)
    today_str = colombia_now.strftime("%Y-%m-%d")

    print(f"\n{'='*60}")
    print(f"Lead SAL Archive -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se ejecutaran cambios (solo lectura)")
    if BACKFILL_MODE:
        print("BACKFILL MODE: procesando TODOS los deals open con SAL date lleno")
    else:
        print(f"Buscando deals con SAL date = {today_str} (hora Colombia)")
    print(f"{'='*60}\n")

    deals = get_deals_with_sal_date(None if BACKFILL_MODE else today_str)

    label = "todos con SAL date" if BACKFILL_MODE else "con SAL date hoy"
    print(f"Deals {label}: {len(deals)}\n")

    if not deals:
        print(f"Sin deals {label}. Nada que hacer.")
        return

    # Construir backup antes de tocar nada
    print("Construyendo backup de leads a archivar...")
    backup = []
    orgs_seen = set()
    for deal in deals:
        org_id, org_name = extract_org_id(deal)
        if not org_id or org_id in orgs_seen:
            continue
        orgs_seen.add(org_id)
        leads = get_active_leads_for_org(org_id)
        for lead in leads:
            backup.append({
                "lead_id": lead["id"],
                "lead_title": lead.get("title", "?"),
                "org_id": org_id,
                "org_name": org_name,
                "deal_id": deal["id"],
                "deal_title": deal.get("title", "?"),
                "sal_date": deal.get(SAL_DATE_KEY, "?"),
            })

    backup_path = "lead_sal_archive_backup.json"
    with open(backup_path, "w") as f:
        json.dump(backup, f, indent=2, ensure_ascii=False)
    print(f"Backup guardado: {backup_path} ({len(backup)} leads)\n")

    total_leads = 0
    total_tasks = 0
    total_errors = 0
    orgs_processed = set()

    for i, deal in enumerate(deals, 1):
        deal_id = deal["id"]
        deal_title = deal.get("title", "?")
        org_id, org_name = extract_org_id(deal)
        sal_date = deal.get(SAL_DATE_KEY, "?")

        print(f"[{i}/{len(deals)}] Deal '{deal_title}' (id={deal_id}) -- SAL date={sal_date}")
        print(f"  Org: {org_id} '{org_name}'")

        if not org_id:
            print("  SKIP: deal sin organizacion vinculada")
            continue

        if org_id in orgs_processed:
            print(f"  SKIP: org {org_id} ya procesada en este run")
            continue
        orgs_processed.add(org_id)

        stats = process_org(org_id, org_name)
        total_leads += stats["leads_archived"]
        total_tasks += stats["tasks_deleted"]
        total_errors += stats["errors"]

    print(f"\n{'='*60}")
    if TEST_MODE:
        print(f"[TEST] Habria archivado {total_leads} leads y eliminado {total_tasks} tareas")
    else:
        print(f"Resumen: {total_leads} leads archivados, {total_tasks} tareas eliminadas, {total_errors} errores")
    print(f"Orgs procesadas: {len(orgs_processed)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
