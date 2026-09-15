#!/usr/bin/env python3
"""
org_contacted_audit.py

Auditoría v2 de First Contact Date.

Scope reducido: solo orgs donde FCD y Prospection Date están en la MISMA
sesión (gap < 60 días) y Prospection Date es de los últimos SCOPE_DAYS días.
Usa la misma lógica exacta que org_contacted_sync:
  - /organizations/{id}/activities con done=1
  - due_date como fecha principal (fallback: marked_as_done_time)
  - tipos calificantes: whatsapp + aircall

Solo lectura — no modifica nada.
"""

import os
import requests
import time
from datetime import date, datetime, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL  = "https://slang.pipedrive.com/api/v1"

PROSPECTION_DATE_KEY  = "2db7aeb0017118ae0c5f9284887c0d55482bbce9"
ORG_FIRST_CONTACT_KEY = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"

WINDOW_DAYS = 60
SCOPE_DAYS  = int(os.environ.get("SCOPE_DAYS", "30"))  # solo orgs con prosp date en últimos N días
MAX_ACT_PAGES = 15

CONTACT_ACTIVITY_TYPES = {
    "whatsapp",
    "aircall_outbound_answered_",
    "aircall_outbound_unanswere",
    "aircall_inbound_answered_c",
    "aircall_missed_call_with_v",
    "aircall_missed_call_withou",
    "aircall_inbound_whatsapp_m",
    "aircall_outbound_whatsapp_",
}

request_count = 0
window_start  = time.time()


def rate_limit():
    global request_count, window_start
    request_count += 1
    if request_count >= 76:
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


def to_date(s):
    if not s:
        return None
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def extract_id(field):
    if field is None:
        return None
    if isinstance(field, dict):
        return field.get("value")
    return field


def get_all_active_leads():
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"archived_status": "not_archived", "limit": 500, "start": start})
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


def fetch_qualifying_contact(org_id, prosp_date, person_id, person_cache):
    """Misma lógica que org_contacted_sync.fetch_org_qualifying_contact:
    - /organizations/{id}/activities con done=1
    - due_date principal, fallback marked_as_done_time
    - filtra ±WINDOW_DAYS del prosp_date
    """
    candidates = []
    act_start = 0
    page = 0
    try:
        while page < MAX_ACT_PAGES:
            ar = api_get(f"organizations/{org_id}/activities",
                         {"done": 1, "limit": 100, "start": act_start})
            page += 1
            for a in (ar.get("data") or []):
                if a.get("type") not in CONTACT_ACTIVITY_TYPES:
                    continue
                d = a.get("due_date") or (a.get("marked_as_done_time") or "")[:10]
                if d and len(str(d)) >= 10:
                    candidates.append(str(d)[:10])
            pag = ar.get("additional_data", {}).get("pagination", {})
            if not pag.get("more_items_in_collection"):
                break
            act_start = pag.get("next_start", act_start + 100)
    except Exception:
        pass

    # Email de la persona
    if person_id:
        if person_id not in person_cache:
            try:
                pr = api_get(f"persons/{person_id}")
                person_cache[person_id] = pr.get("data") or {}
            except Exception:
                person_cache[person_id] = {}
        mail_time = person_cache[person_id].get("last_outgoing_mail_time")
        if mail_time:
            candidates.append(str(mail_time)[:10])

    qualifying = [c for c in candidates if abs((to_date(c) - prosp_date).days) <= WINDOW_DAYS]
    return min(qualifying) if qualifying else None


def main():
    now   = datetime.utcnow()
    today = now.date()
    since = today - timedelta(days=SCOPE_DAYS)

    print(f"\n{'='*70}")
    print(f"Org First Contact Date — Auditoría v2 (misma sesión, scope reducido)")
    print(f"Fecha: {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Scope: orgs con Prospection Date desde {since} (últimos {SCOPE_DAYS} días)")
    print(f"Condición: FCD y Prospection Date en la misma sesión (gap < {WINDOW_DAYS}d)")
    print(f"{'='*70}\n")

    print("Obteniendo leads activos...")
    leads = get_all_active_leads()
    print(f"Leads activos: {len(leads)}")

    # Agrupar por org: prosp date más reciente dentro del scope
    org_latest = {}
    for lead in leads:
        prosp_str = lead.get(PROSPECTION_DATE_KEY)
        if not prosp_str:
            continue
        prosp = to_date(prosp_str)
        if not prosp or prosp < since:
            continue  # fuera del scope temporal
        org_id = extract_id(lead.get("organization_id"))
        if not org_id:
            continue
        if org_id not in org_latest or str(prosp_str) > str(org_latest[org_id].get(PROSPECTION_DATE_KEY, "")):
            org_latest[org_id] = lead

    print(f"Orgs en scope (prosp últimos {SCOPE_DAYS}d): {len(org_latest)}\n")

    wrong      = []
    missing    = []
    correct    = 0
    diff_session = 0  # FCD de sesión distinta → skip
    no_activity  = 0

    org_cache    = {}
    person_cache = {}
    total = len(org_latest)

    for i, (org_id, lead) in enumerate(org_latest.items(), 1):
        if i % 20 == 0:
            print(f"  Procesando {i}/{total}...")

        prosp_str = lead.get(PROSPECTION_DATE_KEY)
        prosp     = to_date(prosp_str)

        # Obtener org
        try:
            if org_id not in org_cache:
                resp = api_get(f"organizations/{org_id}")
                org_cache[org_id] = resp.get("data") or {}
            org = org_cache[org_id]
        except Exception:
            continue

        current_fcd = (org.get(ORG_FIRST_CONTACT_KEY) or "")[:10] or None
        org_name    = org.get("name", str(org_id))

        # Saltar si FCD es de sesión diferente (gap >= 60d) — son válidos
        if current_fcd:
            gap = abs((prosp - to_date(current_fcd)).days)
            if gap >= WINDOW_DAYS:
                diff_session += 1
                continue

        person_id    = extract_id(lead.get("person_id"))
        expected_fcd = fetch_qualifying_contact(org_id, prosp, person_id, person_cache)

        if not expected_fcd:
            no_activity += 1
            continue

        if not current_fcd:
            missing.append({
                "org_id":       org_id,
                "org_name":     org_name,
                "prosp_date":   prosp_str,
                "expected_fcd": expected_fcd,
            })
        elif current_fcd != expected_fcd:
            diff = (to_date(expected_fcd) - to_date(current_fcd)).days
            wrong.append({
                "org_id":       org_id,
                "org_name":     org_name,
                "prosp_date":   prosp_str,
                "current_fcd":  current_fcd,
                "expected_fcd": expected_fcd,
                "diff_days":    diff,
            })
        else:
            correct += 1

    # Reporte
    print(f"\n{'='*70}")
    print(f"RESUMEN")
    print(f"  Orgs en scope:              {total}")
    print(f"  Skipped (sesión distinta):  {diff_session}")
    print(f"  Sin actividad calificante:  {no_activity}")
    print(f"  FCD correcto:               {correct}")
    print(f"  FCD incorrecto:             {len(wrong)}")
    print(f"  FCD vacío (con actividad):  {len(missing)}")
    print(f"{'='*70}\n")

    if wrong:
        print(f"─── FCD INCORRECTO ({len(wrong)} orgs) ───")
        for w in sorted(wrong, key=lambda x: abs(x["diff_days"]), reverse=True):
            sign = "+" if w["diff_days"] > 0 else ""
            print(f"  [{w['org_id']}] {w['org_name']}")
            print(f"    Prosp: {w['prosp_date']} | FCD: {w['current_fcd']} → esperado: {w['expected_fcd']} ({sign}{w['diff_days']}d)")
        print()

    if missing:
        print(f"─── FCD VACÍO CON ACTIVIDAD ({len(missing)} orgs) ───")
        for m in missing:
            print(f"  [{m['org_id']}] {m['org_name']} — Prosp: {m['prosp_date']} | Esperado: {m['expected_fcd']}")
        print()

    if not wrong and not missing:
        print("Sin discrepancias detectadas.")


if __name__ == "__main__":
    main()
