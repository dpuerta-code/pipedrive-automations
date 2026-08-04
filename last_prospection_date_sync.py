#!/usr/bin/env python3
"""
last_prospection_date_sync.py

Actualiza "Last Prospection Date" en organizaciones según los leads activos.

Reglas (se evalúan cuando hay un lead con Prospection Date más reciente que
el Last Prospection Date actual de la org):

  NO actualizar si:
    - El nuevo Prospection Date está dentro de los 60 días siguientes al
      Last Prospection Date actual, Y
    - El owner del nuevo lead es el mismo que el del lead anterior.

  SÍ actualizar si:
    - Han pasado >= 60 días, O
    - El owner del nuevo lead es diferente al del lead anterior.

TEST_MODE=true  → solo lectura.
Cron: lunes-viernes, mismos horarios que Org Contacted Sync.
"""

import os
import requests
import time
from datetime import datetime, date, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

PROSPECTION_DATE_KEY = "2db7aeb0017118ae0c5f9284887c0d55482bbce9"   # Prospection Date (lead)
ORG_LAST_PROSPECTION_KEY = "2fd7273aed05f1cbab54ec64bbdb7e5dfe69fd22"  # Last Prospection Date (org)

SAME_OWNER_WINDOW_DAYS = 60
LOOKBACK_DAYS = 3  # cuántos días atrás buscar leads nuevos para decidir qué orgs procesar

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
    y, m, d = map(int, str(s)[:10].split("-"))
    return date(y, m, d)


def get_owner_id(lead):
    owner = lead.get("owner_id")
    if isinstance(owner, dict):
        return owner.get("id")
    return owner


def get_owner_name(lead):
    owner = lead.get("owner_id")
    if isinstance(owner, dict):
        return owner.get("name", str(get_owner_id(lead)))
    return str(owner)


def get_all_active_leads():
    """Todos los leads activos con Prospection Date y org vinculada."""
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"limit": 500, "start": start, "archived_status": "not_archived"})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [l for l in leads if l.get(PROSPECTION_DATE_KEY) and l.get("organization_id")]


def get_org(org_id, org_cache):
    if org_id not in org_cache:
        try:
            resp = api_get(f"organizations/{org_id}")
            org_cache[org_id] = resp.get("data") or {}
        except Exception:
            org_cache[org_id] = {}
    return org_cache[org_id]


def main():
    print(f"\n{'='*60}")
    print(f"Last Prospection Date Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if TEST_MODE:
        print("MODO TEST: no se ejecutarán cambios")
    print(f"{'='*60}\n")

    # 1. Obtener todos los leads activos con Prospection Date
    all_leads = get_all_active_leads()
    print(f"Leads activos con Prospection Date: {len(all_leads)}")

    # 2. Determinar qué orgs procesar: solo las que tienen un lead nuevo
    #    creado en los últimos LOOKBACK_DAYS días
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent_org_ids = {
        l["organization_id"]
        for l in all_leads
        if (l.get("add_time") or "")[:10] >= cutoff
    }
    print(f"Orgs con leads nuevos en los últimos {LOOKBACK_DAYS} días: {len(recent_org_ids)}\n")

    if not recent_org_ids:
        print("Nada que procesar.")
        return

    # Índice de leads por org para búsqueda rápida
    leads_by_org = {}
    for l in all_leads:
        leads_by_org.setdefault(l["organization_id"], []).append(l)

    org_cache = {}
    stats = {"updated": 0, "skipped_same_owner": 0, "skipped_no_change": 0, "errors": 0}

    for org_id in sorted(recent_org_ids):
        org = get_org(org_id, org_cache)
        org_name = org.get("name", str(org_id))
        current_lpd = org.get(ORG_LAST_PROSPECTION_KEY)
        current_lpd_str = str(current_lpd)[:10] if current_lpd else None

        # Leads de esta org ordenados por Prospection Date DESC
        org_leads = sorted(
            leads_by_org.get(org_id, []),
            key=lambda l: str(l.get(PROSPECTION_DATE_KEY) or ""),
            reverse=True,
        )

        if not org_leads:
            continue

        new_lead = org_leads[0]
        new_pd_str = str(new_lead[PROSPECTION_DATE_KEY])[:10]

        # Si el Last Prospection Date ya está en el valor correcto, skip
        if current_lpd_str and new_pd_str <= current_lpd_str:
            stats["skipped_no_change"] += 1
            continue

        new_owner_id = get_owner_id(new_lead)
        new_owner_name = get_owner_name(new_lead)

        # Si no hay Last Prospection Date previo → simplemente establecerlo
        if not current_lpd_str:
            if TEST_MODE:
                print(f"Org '{org_name}' ({org_id}): [TEST] Establecería Last Prospection Date = {new_pd_str} (primer valor)")
                stats["updated"] += 1
            else:
                try:
                    resp = api_patch(f"organizations/{org_id}", {ORG_LAST_PROSPECTION_KEY: new_pd_str})
                    if resp.get("success"):
                        print(f"Org '{org_name}' ({org_id}): Last Prospection Date = {new_pd_str} (primer valor)")
                        org_cache[org_id][ORG_LAST_PROSPECTION_KEY] = new_pd_str
                        stats["updated"] += 1
                    else:
                        print(f"Org '{org_name}' ({org_id}): ERROR al actualizar: {resp}")
                        stats["errors"] += 1
                except Exception as e:
                    print(f"Org '{org_name}' ({org_id}): ERROR: {e}")
                    stats["errors"] += 1
            continue

        # Buscar el "lead anterior": el más reciente con Prospection Date <= current_lpd
        old_lead = next(
            (l for l in org_leads if l["id"] != new_lead["id"]
             and str(l.get(PROSPECTION_DATE_KEY) or "")[:10] <= current_lpd_str),
            None,
        )
        old_owner_id = get_owner_id(old_lead) if old_lead else None
        old_owner_name = get_owner_name(old_lead) if old_lead else "?"

        days_diff = (to_date(new_pd_str) - to_date(current_lpd_str)).days

        # Aplicar regla
        if days_diff < SAME_OWNER_WINDOW_DAYS and new_owner_id == old_owner_id:
            print(
                f"Org '{org_name}' ({org_id}): SKIP — mismo rep ({new_owner_name}), "
                f"{days_diff} días desde last prospection ({current_lpd_str} → {new_pd_str})"
            )
            stats["skipped_same_owner"] += 1
            continue

        reason = f"{days_diff} días" if days_diff >= SAME_OWNER_WINDOW_DAYS else f"rep diferente ({old_owner_name} → {new_owner_name})"
        if TEST_MODE:
            print(
                f"Org '{org_name}' ({org_id}): [TEST] Actualizaría {current_lpd_str} → {new_pd_str} "
                f"({reason})"
            )
            stats["updated"] += 1
        else:
            try:
                resp = api_patch(f"organizations/{org_id}", {ORG_LAST_PROSPECTION_KEY: new_pd_str})
                if resp.get("success"):
                    print(
                        f"Org '{org_name}' ({org_id}): {current_lpd_str} → {new_pd_str} ({reason})"
                    )
                    org_cache[org_id][ORG_LAST_PROSPECTION_KEY] = new_pd_str
                    stats["updated"] += 1
                else:
                    print(f"Org '{org_name}' ({org_id}): ERROR al actualizar: {resp}")
                    stats["errors"] += 1
            except Exception as e:
                print(f"Org '{org_name}' ({org_id}): ERROR: {e}")
                stats["errors"] += 1

    print(f"\n{'='*60}")
    prefix = "[TEST] " if TEST_MODE else ""
    print(
        f"{prefix}Resumen: {stats['updated']} actualizadas, "
        f"{stats['skipped_same_owner']} skip (mismo rep <{SAME_OWNER_WINDOW_DAYS}d), "
        f"{stats['skipped_no_change']} sin cambio, "
        f"{stats['errors']} errores"
    )
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
