#!/usr/bin/env python3
"""
org_fcd_owner_sync.py

Detecta orgs donde el FCD pertenece a un rep anterior al owner actual:
  - Pagina organizations de Pipedrive para obtener candidatos con LPD reciente y FCD antiguo
  - Para cada org, compara la primera actividad calificante del owner actual vs FCD
  - Si el owner actual contactó por primera vez DESPUÉS del FCD → actualiza FCD

Corre todos los días 5am hora Colombia.
TEST_MODE=true → solo lectura.
"""

import os
import time
import requests
from datetime import date, timedelta, datetime

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL  = "https://slang.pipedrive.com/api/v1"

ORG_FIRST_CONTACT_KEY       = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"
ORG_LAST_PROSPECTION_KEY    = "2fd7273aed05f1cbab54ec64bbdb7e5dfe69fd22"
ORG_COUNT_FIRST_CONTACT_KEY = "e117d76508f5bdc87d55c35f3c30dacd10c6f7d9"
ORG_COUNT_ONE_OPTION        = 1413
ORG_COUNT_ZERO_OPTION       = 1412

# Ventana: orgs con LPD en los últimos N días y FCD antes del inicio de esa ventana
LOOKBACK_DAYS = 90

CONTACT_ACTIVITY_TYPES = {
    "whatsapp", "aircall_outbound_answered_", "aircall_outbound_unanswere",
    "aircall_inbound_answered_c", "aircall_missed_call_with_v",
    "aircall_missed_call_withou", "aircall_inbound_whatsapp_m",
    "aircall_outbound_whatsapp_",
}

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
MAX_ACT_PAGES = 20

req_count = 0
win_start = time.time()


def rate_limit():
    global req_count, win_start
    req_count += 1
    if req_count >= 76:
        elapsed = time.time() - win_start
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        req_count = 0
        win_start = time.time()


def api_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": API_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{BASE_URL}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def api_put_org(org_id, data):
    rate_limit()
    r = requests.put(f"{BASE_URL}/organizations/{org_id}",
                     params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_candidate_orgs(lpd_from, lpd_to, fcd_before):
    """
    Pagina todas las orgs de Pipedrive y filtra:
    LPD en [lpd_from, lpd_to] Y FCD existe Y FCD < fcd_before.
    """
    candidate_ids = []
    start = 0
    page = 0
    while True:
        page += 1
        resp = api_get("organizations", {"limit": 500, "start": start})
        orgs = resp.get("data") or []
        for org in orgs:
            lpd = str(org.get(ORG_LAST_PROSPECTION_KEY) or "")[:10]
            fcd = str(org.get(ORG_FIRST_CONTACT_KEY) or "")[:10]
            if not lpd or not fcd or len(lpd) < 10 or len(fcd) < 10:
                continue
            if lpd_from <= lpd <= lpd_to and fcd < fcd_before:
                candidate_ids.append(org["id"])
        pag = resp.get("additional_data", {}).get("pagination", {})
        if not pag.get("more_items_in_collection"):
            break
        start = pag.get("next_start", start + 500)
    return candidate_ids


def get_org(org_id):
    resp = api_get(f"organizations/{org_id}")
    return resp.get("data") or {}


def get_qualifying_activities(org_id):
    results = []
    start, page = 0, 0
    while page < MAX_ACT_PAGES:
        page += 1
        resp = api_get(f"organizations/{org_id}/activities",
                       {"done": 1, "limit": 100, "start": start})
        for a in (resp.get("data") or []):
            if a.get("type") not in CONTACT_ACTIVITY_TYPES:
                continue
            raw = a.get("due_date") or (a.get("marked_as_done_time") or "")[:10]
            d_str = str(raw)[:10]
            if not d_str or len(d_str) < 10:
                continue
            try:
                date.fromisoformat(d_str)
            except ValueError:
                continue
            uid = a.get("user_id")
            uid = uid.get("id") if isinstance(uid, dict) else uid
            results.append((d_str, uid))
        pag = resp.get("additional_data", {}).get("pagination", {})
        if not pag.get("more_items_in_collection"):
            break
        start = pag.get("next_start", start + 100)
    results.sort(key=lambda x: x[0])
    return results


def main():
    today     = date.today()
    lpd_from  = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    lpd_to    = today.isoformat()
    fcd_before = lpd_from  # FCD debe ser anterior a la ventana de LPD

    print(f"\n{'='*70}")
    print(f"Org FCD Owner Sync — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"Ventana LPD: {lpd_from} a {lpd_to} | FCD < {fcd_before}")
    print(f"TEST_MODE: {'SI (solo lectura)' if TEST_MODE else 'NO — aplicando cambios'}")
    print(f"{'='*70}\n")

    print("Buscando candidatos en Pipedrive (paginando orgs)...")
    candidate_ids = get_candidate_orgs(lpd_from, lpd_to, fcd_before)
    print(f"Candidatos encontrados: {len(candidate_ids)}\n")

    fixes        = []
    no_activity  = []
    already_ok   = 0

    for org_id in candidate_ids:
        org = get_org(org_id)
        if not org:
            continue

        org_name = org.get("name", str(org_id))
        fcd = str(org.get(ORG_FIRST_CONTACT_KEY) or "")[:10]
        if not fcd:
            continue

        owner_id = org.get("owner_id")
        if isinstance(owner_id, dict):
            owner_id = owner_id.get("id")

        activities = get_qualifying_activities(org_id)
        owner_acts = [d for d, uid in activities if uid == owner_id]

        if not owner_acts:
            no_activity.append((org_id, org_name, fcd))
            continue

        first_owner_act = min(owner_acts)

        if first_owner_act > fcd:
            fixes.append((org_id, org_name, fcd, first_owner_act))
        else:
            already_ok += 1

    print(f"{'Org ID':>8}  {'Nombre':38s}  {'FCD actual':12s}  {'Nuevo FCD':12s}")
    print("-" * 80)
    for org_id, org_name, old_fcd, new_fcd in fixes:
        print(f"  {org_id:>8}  {org_name:38s}  {old_fcd:12s}  {new_fcd}")

    print(f"\nCandidatos a corregir:             {len(fixes)}")
    print(f"FCD ya correcto:                   {already_ok}")
    print(f"Owner sin actividades (limpiar):   {len(no_activity)}")

    if TEST_MODE:
        print("\n[TEST] No se aplicaron cambios.")
        return

    ok, errors = 0, 0

    for org_id, org_name, old_fcd, new_fcd in fixes:
        try:
            api_put_org(org_id, {
                ORG_FIRST_CONTACT_KEY: new_fcd,
                ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ONE_OPTION,
            })
            print(f"  OK    [{org_id}] {org_name:38s}  {old_fcd} → {new_fcd}")
            ok += 1
        except Exception as e:
            print(f"  ERR   [{org_id}] {org_name}: {e}")
            errors += 1

    for org_id, org_name, fcd in no_activity:
        try:
            api_put_org(org_id, {
                ORG_FIRST_CONTACT_KEY: None,
                ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ZERO_OPTION,
            })
            print(f"  CLEAR [{org_id}] {org_name:38s}  {fcd} → (limpiado)")
            ok += 1
        except Exception as e:
            print(f"  ERR   [{org_id}] {org_name}: {e}")
            errors += 1

    print(f"\n{'='*70}")
    print(f"Resumen: {ok} actualizadas, {errors} errores")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
