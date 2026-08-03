#!/usr/bin/env python3
"""
Backfill unico de "Org First Contact Date" para leads activos que quedaron
pendientes porque org_contacted_sync.py solo revisa actividad de los
ultimos 3 dias en cada corrida (si el contacto real ocurrio antes, nunca
se vuelve a chequear).

La lista de leads a corregir (lead_id, organization_id, contact_date) se
calculo aparte contra el historial COMPLETO de actividades (WhatsApp/
Aircall/correo) dentro de +/-60 dias del Prospection Date de cada lead,
y se guardo en leads_to_backfill.json.

Este script solo aplica esos valores via API (con re-chequeo write-once
justo antes de escribir) y propaga a la organizacion si su First Contact
Date esta vacio, igual que org_contacted_sync.py.

TEST_MODE=true -> solo procesa hasta MAX_LEADS_TEST_MODE leads.
"""

import os
import json
import time
import requests

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

CONTACT_DATE_KEY = "d81b6ab138f59f521821c5b29f1dc389b7a0cad4"      # Org First Contact Date (lead)
ORG_FIRST_CONTACT_KEY = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"  # First Contact Date (org)
ORG_COUNT_FIRST_CONTACT_KEY = "e117d76508f5bdc87d55c35f3c30dacd10c6f7d9"
ORG_COUNT_ONE_OPTION = 1413  # "1"

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


def api_get(endpoint):
    rate_limit()
    r = requests.get(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, timeout=30)
    r.raise_for_status()
    return r.json()


def api_patch(endpoint, data):
    rate_limit()
    r = requests.patch(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def api_put(endpoint, data):
    rate_limit()
    r = requests.put(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def propagate_to_org(org_id, contact_date, org_cache):
    if org_id not in org_cache:
        try:
            resp = api_get(f"organizations/{org_id}")
            org_cache[org_id] = resp.get("data") or {}
        except Exception:
            return False
    org = org_cache[org_id]
    if org.get(ORG_FIRST_CONTACT_KEY):
        return False
    try:
        api_put(f"organizations/{org_id}", {
            ORG_FIRST_CONTACT_KEY: contact_date,
            ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ONE_OPTION,
        })
        org_cache[org_id][ORG_FIRST_CONTACT_KEY] = contact_date
        return True
    except Exception as e:
        print(f"  ERROR propagando a org {org_id}: {e}")
        return False


def main():
    with open("leads_to_backfill.json") as f:
        records = json.load(f)

    print(f"\n{'='*60}")
    print(f"Org Contacted Backfill (historico completo)")
    print(f"Leads a procesar en el archivo: {len(records)}")
    if TEST_MODE:
        records = records[:MAX_LEADS_TEST_MODE]
        print(f"MODO TEST: solo se procesaran {len(records)}")
    print(f"{'='*60}\n")

    org_cache = {}
    stats = {"updated": 0, "already_filled": 0, "propagated_to_org": 0, "error": 0}
    backup_log = []

    for i, rec in enumerate(records, 1):
        lead_id = rec["lead_id"]
        org_id = rec["organization_id"]
        contact_date = rec["contact_date"]

        try:
            fresh = api_get(f"leads/{lead_id}")
            current = fresh.get("data", {}).get(CONTACT_DATE_KEY)
            if current:
                print(f"[{i}/{len(records)}] lead {lead_id}: ya tiene fecha ({current}), se omite.")
                stats["already_filled"] += 1
                continue

            resp = api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: contact_date})
            if resp.get("success"):
                print(f"[{i}/{len(records)}] lead {lead_id}: Org First Contact Date = {contact_date}")
                stats["updated"] += 1
                backup_log.append({"lead_id": lead_id, "old_value": None, "new_value": contact_date})
                if propagate_to_org(org_id, contact_date, org_cache):
                    stats["propagated_to_org"] += 1
                    print(f"    -> tambien se lleno en la organizacion {org_id}")
            else:
                print(f"[{i}/{len(records)}] lead {lead_id}: respuesta sin exito: {resp}")
                stats["error"] += 1
        except Exception as e:
            print(f"[{i}/{len(records)}] ERROR en lead {lead_id}: {e}")
            stats["error"] += 1

    with open("backfill_v2_result_log.json", "w") as f:
        json.dump(backup_log, f)

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['updated']} actualizados, {stats['propagated_to_org']} propagados a su org, "
          f"{stats['already_filled']} ya tenian fecha, {stats['error']} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
