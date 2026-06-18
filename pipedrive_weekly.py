#!/usr/bin/env python3
"""
Script semanal: busca deals perdidos en los últimos 7 días cuyo campo
"SQL date" NO esté vacío. Para cada org afectada, crea o actualiza la
nota pinneada con el resumen enriquecido de TODOS sus deals.

Uso manual:  python3 pipedrive_weekly.py
"""

import os
import requests
import time
import sys
from datetime import datetime, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

# Custom field hashes
NEED_DESC = "c668a9a93dfbd0429ea61b8e53cff197931189ab"
LOST_DETAIL = "a1c39a241467b77d8b611f594166db936b7c1c17"
DECISION = "39d391ea4c335cfa0fae9af9d3ea09ccdc506aee"
TIMING = "28f2f450dcb76339a0c98c6a412de9e4170f1992"
BUDGET = "fc9117d2b745a41c2054559c8177766960948df1"
INDUSTRY = "bc1cace2181e0401258e6feb2df95b12912ea615"
CONTACT_ROLE = "53b457205e91e0b5f37fb7ef2fb11cbfcae5127a"
BLOCKER = "637434692c9f94b49f2da304b894f68f5c0f3f43"
SQL_DATE = "8efe73cd06135bcc6e31a5c427c533c29cfb8d97"

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
    r = requests.post(
        f"{BASE_URL}/{endpoint}",
        params={"api_token": API_TOKEN},
        json=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def api_put(endpoint, data):
    rate_limit()
    r = requests.put(
        f"{BASE_URL}/{endpoint}",
        params={"api_token": API_TOKEN},
        json=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def clean(val):
    if val is None:
        return ""
    s = str(val).strip()
    if s in ("0", "0.0", "None", "null", "N/A", ""):
        return ""
    return s


def get_lost_deals_this_week():
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"Buscando deals perdidos desde {cutoff} con SQL date no vacío...")

    qualifying = []
    start = 0

    while True:
        resp = api_get("deals", {
            "status": "lost",
            "sort": "lost_time DESC",
            "start": start,
            "limit": 500,
        })
        deals = resp.get("data") or []
        if not deals:
            break

        for d in deals:
            lost_time = d.get("lost_time", "") or ""
            if lost_time < cutoff:
                return qualifying

            sql_date = d.get(SQL_DATE)
            if sql_date:
                org_data = d.get("org_id")
                org_id = org_data.get("value") if isinstance(org_data, dict) else org_data
                if org_id:
                    qualifying.append({
                        "deal_id": d["id"],
                        "deal_title": d.get("title", ""),
                        "org_id": int(org_id),
                        "lost_time": lost_time,
                        "sql_date": sql_date,
                    })

        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break

    return qualifying


def get_all_org_deals(org_id):
    deals = []
    start = 0
    while True:
        resp = api_get(
            f"organizations/{org_id}/deals",
            {"start": start, "limit": 100, "status": "all_not_deleted"},
        )
        if resp.get("success") and resp.get("data"):
            deals.extend(resp["data"])
            if resp.get("additional_data", {}).get("pagination", {}).get("more_items_in_collection"):
                start = resp["additional_data"]["pagination"]["next_start"]
            else:
                break
        else:
            break
    return deals


def get_org_name(org_id):
    try:
        resp = api_get(f"organizations/{org_id}")
        if resp.get("success"):
            return resp["data"].get("name", "N/A")
    except Exception:
        pass
    return "N/A"


def generate_note_html(org_name, deals):
    if not deals:
        return f"<p><strong>{org_name} | 0 deal(s)</strong></p>"

    open_c = sum(1 for d in deals if d.get("status") == "open")
    won_c = sum(1 for d in deals if d.get("status") == "won")
    lost_c = sum(1 for d in deals if d.get("status") == "lost")

    html = f"<p><strong>{org_name} | {len(deals)} deal(s) (Open:{open_c} Won:{won_c} Lost:{lost_c})</strong></p>"
    html += "<ul>"

    for d in deals:
        status = (d.get("status") or "").upper()
        title = d.get("title", "N/A")
        val = d.get("value", 0) or 0
        curr = d.get("currency", "USD")
        person = d.get("person_name") or "N/A"
        role = clean(d.get(CONTACT_ROLE))

        line = f"<li><strong>{title}</strong> [{status}] ${val:,.0f} {curr}"
        line += f" | Contacto: {person}"
        if role:
            line += f" ({role})"

        ind = clean(d.get(INDUSTRY))
        if ind:
            line += f" | {ind}"

        need = clean(d.get(NEED_DESC))
        if need:
            line += f"<br/>Necesidad: {need[:250]}"

        tm = clean(d.get(TIMING))
        if len(tm) > 2:
            line += f"<br/>Timing: {tm[:150]}"

        bg = clean(d.get(BUDGET))
        if len(bg) > 2:
            line += f"<br/>Presupuesto: {bg[:150]}"

        dec = clean(d.get(DECISION))
        if len(dec) > 2:
            line += f"<br/>Proceso decisión: {dec[:150]}"

        blk = clean(d.get(BLOCKER))
        if len(blk) > 2:
            line += f"<br/>Bloqueador: {blk[:150]}"

        if status == "LOST":
            lr = clean(d.get("lost_reason"))
            detail = clean(d.get(LOST_DETAIL))
            if lr or detail:
                line += f"<br/><b>Razón de pérdida: {lr}</b>"
                if detail:
                    line += f" — {detail[:200]}"

        line += "</li>"
        html += line

    html += "</ul>"
    return html


def find_existing_pinned_note(org_id):
    try:
        resp = api_get("notes", {
            "org_id": org_id,
            "pinned_to_organization_flag": 1,
            "limit": 50,
        })
        notes = resp.get("data") or []
        for n in notes:
            if n.get("pinned_to_organization_flag"):
                content = n.get("content", "")
                if "deal(s)" in content and "(Open:" in content:
                    return n["id"]
    except Exception:
        pass
    return None


def create_or_update_note(org_id, html):
    existing_id = find_existing_pinned_note(org_id)

    if existing_id:
        try:
            resp = api_put(f"notes/{existing_id}", {"content": html})
            if resp.get("success"):
                return existing_id, "updated"
        except Exception as e:
            print(f"  Error actualizando nota {existing_id} para org {org_id}: {e}")
            return None, "error"
    else:
        try:
            resp = api_post("notes", {
                "org_id": org_id,
                "content": html,
                "pinned_to_organization_flag": 1,
            })
            if resp.get("success"):
                return resp["data"]["id"], "created"
        except Exception as e:
            print(f"  Error creando nota para org {org_id}: {e}")
            return None, "error"

    return None, "error"


def main():
    print(f"\n{'='*60}")
    print(f"Pipedrive Weekly Notes — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    qualifying = get_lost_deals_this_week()

    if not qualifying:
        print("No se encontraron deals perdidos esta semana con SQL date.")
        return

    org_ids = sorted(set(d["org_id"] for d in qualifying))
    print(f"Deals perdidos esta semana con SQL date: {len(qualifying)}")
    print(f"Organizaciones afectadas: {len(org_ids)}")
    print()

    for q in qualifying:
        print(f"  Deal #{q['deal_id']} '{q['deal_title']}' → org {q['org_id']} (lost: {q['lost_time']}, sql_date: {q['sql_date']})")
    print()

    stats = {"updated": 0, "created": 0, "error": 0, "skipped": 0}

    for i, org_id in enumerate(org_ids):
        print(f"[{i+1}/{len(org_ids)}] Org {org_id}...", end=" ")

        org_name = get_org_name(org_id)
        deals = get_all_org_deals(org_id)

        if not deals:
            print(f"SKIP ({org_name} — sin deals)")
            stats["skipped"] += 1
            continue

        html = generate_note_html(org_name, deals)
        note_id, action = create_or_update_note(org_id, html)

        if action in ("updated", "created"):
            print(f"{action.upper()} ({org_name} — {len(deals)} deals, note #{note_id})")
            stats[action] += 1
        else:
            print(f"ERROR ({org_name})")
            stats["error"] += 1

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['created']} creadas, {stats['updated']} actualizadas, "
          f"{stats['skipped']} sin deals, {stats['error']} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
