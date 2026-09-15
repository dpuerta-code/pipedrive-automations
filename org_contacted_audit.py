#!/usr/bin/env python3
"""
org_contacted_audit.py

Auditoría de First Contact Date: compara el FCD actual de cada org activa
con la fecha más temprana de actividad calificante (WhatsApp/Aircall/correo)
dentro de ±60 días del Prospection Date del lead activo más reciente.

Solo lectura — no modifica nada.
Genera un reporte de discrepancias en stdout.
"""

import os
import requests
import time
from datetime import date, datetime, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL  = "https://slang.pipedrive.com/api/v1"

PROSPECTION_DATE_KEY  = "2db7aeb0017118ae0c5f9284887c0d55482bbce9"
ORG_FIRST_CONTACT_KEY = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"
ORG_LAST_PROSPECTION_KEY = "2fd7273aed05f1cbab54ec64bbdb7e5dfe69fd22"

WINDOW_DAYS = 60

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
    return date.fromisoformat(str(s)[:10])


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


def get_org(org_id):
    resp = api_get(f"organizations/{org_id}")
    return resp.get("data") or {}


def get_person(person_id):
    resp = api_get(f"persons/{person_id}")
    return resp.get("data") or {}


def extract_id(field):
    if field is None:
        return None
    if isinstance(field, dict):
        return field.get("value")
    return field


def get_org_qualifying_activities(org_id, prosp_date):
    """Busca actividades calificantes de la org dentro de ±WINDOW_DAYS del Prospection Date."""
    cutoff_low  = prosp_date - timedelta(days=WINDOW_DAYS)
    cutoff_high = prosp_date + timedelta(days=WINDOW_DAYS)

    dates = []
    start = 0
    pages = 0
    while pages < 20:
        pages += 1
        resp = api_get("activities", {
            "org_id": org_id,
            "limit":  500,
            "start":  start,
        })
        data = resp.get("data") or []
        if not data:
            break
        for a in data:
            if a.get("type") not in CONTACT_ACTIVITY_TYPES:
                continue
            d_str = (a.get("due_date") or a.get("add_time") or "")[:10]
            if not d_str:
                continue
            d = to_date(d_str)
            if d and cutoff_low <= d <= cutoff_high:
                dates.append(d_str)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return dates


def main():
    now = datetime.utcnow()
    print(f"\n{'='*70}")
    print(f"Org First Contact Date — Auditoría de discrepancias")
    print(f"Fecha: {now.strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*70}\n")

    print("Obteniendo leads activos...")
    leads = get_all_active_leads()
    print(f"Leads activos: {len(leads)}")

    # Agrupar por org: tomar el lead con Prospection Date más reciente por org
    org_latest: dict = {}  # org_id -> lead con prosp date más reciente
    for lead in leads:
        prosp = lead.get(PROSPECTION_DATE_KEY)
        if not prosp:
            continue
        org_id = extract_id(lead.get("organization_id"))
        if not org_id:
            continue
        if org_id not in org_latest or str(prosp) > str(org_latest[org_id].get(PROSPECTION_DATE_KEY, "")):
            org_latest[org_id] = lead

    print(f"Orgs activas con Prospection Date: {len(org_latest)}\n")

    wrong   = []  # FCD incorrecto (distinto al esperado)
    missing = []  # Tiene actividad calificante pero FCD vacío
    correct = 0
    no_activity = 0

    org_cache    = {}
    person_cache = {}
    total = len(org_latest)

    for i, (org_id, lead) in enumerate(org_latest.items(), 1):
        if i % 50 == 0:
            print(f"  Procesando {i}/{total}...")

        prosp_str = lead.get(PROSPECTION_DATE_KEY)
        prosp     = to_date(prosp_str)
        if not prosp:
            continue

        # Org actual
        try:
            if org_id not in org_cache:
                org_cache[org_id] = get_org(org_id)
            org = org_cache[org_id]
        except Exception:
            continue

        current_fcd = (org.get(ORG_FIRST_CONTACT_KEY) or "")[:10] or None
        org_name    = org.get("name", str(org_id))

        # Actividades calificantes
        act_dates = get_org_qualifying_activities(org_id, prosp)

        # Email de la persona del lead
        person_id = extract_id(lead.get("person_id"))
        if person_id:
            try:
                if person_id not in person_cache:
                    person_cache[person_id] = get_person(person_id)
                mail_time = person_cache[person_id].get("last_outgoing_mail_time")
                if mail_time:
                    mail_date = to_date(mail_time)
                    if mail_date and abs((mail_date - prosp).days) <= WINDOW_DAYS:
                        act_dates.append(str(mail_date))
            except Exception:
                pass

        if not act_dates:
            no_activity += 1
            continue

        expected_fcd = min(act_dates)

        if not current_fcd:
            missing.append({
                "org_id":        org_id,
                "org_name":      org_name,
                "prosp_date":    prosp_str,
                "expected_fcd":  expected_fcd,
                "current_fcd":   "(vacío)",
            })
        elif current_fcd != expected_fcd:
            wrong.append({
                "org_id":       org_id,
                "org_name":     org_name,
                "prosp_date":   prosp_str,
                "current_fcd":  current_fcd,
                "expected_fcd": expected_fcd,
                "diff_days":    (to_date(expected_fcd) - to_date(current_fcd)).days,
            })
        else:
            correct += 1

    # Reporte
    print(f"\n{'='*70}")
    print(f"RESUMEN")
    print(f"  Correctos:             {correct}")
    print(f"  Sin actividad conocida:{no_activity}")
    print(f"  FCD incorrecto:        {len(wrong)}")
    print(f"  FCD vacío (con act.):  {len(missing)}")
    print(f"{'='*70}\n")

    if wrong:
        print(f"─── FCD INCORRECTO ({len(wrong)} orgs) ───")
        for w in sorted(wrong, key=lambda x: abs(x["diff_days"]), reverse=True):
            diff = w["diff_days"]
            sign = "+" if diff > 0 else ""
            print(f"  [{w['org_id']}] {w['org_name']}")
            print(f"    Prosp: {w['prosp_date']} | FCD actual: {w['current_fcd']} | Debería ser: {w['expected_fcd']} ({sign}{diff}d)")
        print()

    if missing:
        print(f"─── FCD VACÍO CON ACTIVIDAD ({len(missing)} orgs) ───")
        for m in missing:
            print(f"  [{m['org_id']}] {m['org_name']} — Prosp: {m['prosp_date']} | Debería ser: {m['expected_fcd']}")
        print()

    if not wrong and not missing:
        print("No se encontraron discrepancias.")


if __name__ == "__main__":
    main()
