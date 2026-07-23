#!/usr/bin/env python3
"""
DESACTIVADO (2026-07-23): el workflow de GitHub Actions se eliminó.
Reemplazado por org_contacted_sync.py, que marca First Contact Date con
la fecha real de contacto (WhatsApp/Aircall/correo) en vez de la fecha
en que corre el script, y no depende del filtro 47734 (que tenía fechas
fijas hardcodeadas). Se deja este archivo como referencia histórica.

Para todas las organizaciones del filtro 47734, marca:
  - "Count - Org First Contact Date" = 1
  - "First Contact Date" = fecha de hoy (YYYY-MM-DD)
"""

import os
import requests
import time
from datetime import datetime, date

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL  = "https://slang.pipedrive.com/api/v1"
FILTER_ID = 47734

COUNT_KEY = "e117d76508f5bdc87d55c35f3c30dacd10c6f7d9"  # Count - Org First Contact Date
DATE_KEY  = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"  # First Contact Date

TODAY = date.today().strftime("%Y-%m-%d")

_req_count = 0
_req_win   = time.time()

def rate_limit():
    global _req_count, _req_win
    _req_count += 1
    if _req_count >= 80:
        elapsed = time.time() - _req_win
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        _req_count = 0
        _req_win   = time.time()


def get_orgs_from_filter():
    orgs, start = [], 0
    while True:
        rate_limit()
        r = requests.get(
            f"{BASE_URL}/organizations",
            params={"api_token": API_TOKEN, "filter_id": FILTER_ID,
                    "start": start, "limit": 500},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        page = data.get("data") or []
        orgs.extend(page)
        if not data.get("additional_data", {}).get("pagination", {}).get("more_items_in_collection"):
            break
        start += len(page)
    return orgs


def update_org(org_id):
    rate_limit()
    r = requests.put(
        f"{BASE_URL}/organizations/{org_id}",
        params={"api_token": API_TOKEN},
        json={COUNT_KEY: 1, DATE_KEY: TODAY},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("success", False)


def main():
    print(f"\n{'='*60}")
    print(f"First Contact Date Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Filtro: {FILTER_ID} | Fecha: {TODAY}")
    print(f"{'='*60}\n")

    orgs = get_orgs_from_filter()
    total = len(orgs)
    print(f"Organizaciones en filtro: {total}\n")

    if total == 0:
        print("Sin resultados. Nada que actualizar.")
        return

    ok_count = 0
    err_count = 0
    for i, org in enumerate(orgs, 1):
        org_id   = org["id"]
        org_name = org.get("name", "?")
        print(f"[{i}/{total}] {org_name} (id={org_id})", end=" ... ", flush=True)
        try:
            if update_org(org_id):
                print("OK")
                ok_count += 1
            else:
                print("sin éxito")
                err_count += 1
        except Exception as e:
            print(f"ERROR: {e}")
            err_count += 1

    print(f"\n{'='*60}")
    print(f"Actualizadas: {ok_count} | Errores: {err_count}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
