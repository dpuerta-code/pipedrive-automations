#!/usr/bin/env python3
"""
Busca todas las organizaciones en Pipedrive que tienen el campo
"Organization LinkedIn" completado, consulta el company size vía
Apify (bebity/linkedin-company-scraper), y marca en
"ICP Non Compliance Reason" la opción "Menos de 50 empleados"
si el resultado de LinkedIn indica < 50 empleados.

El campo ICP es multi-select (set): solo agrega la opción 1428,
no sobreescribe las demás opciones que ya tenga.
"""

import os
import time
import requests
from datetime import datetime

# ── Credenciales ────────────────────────────────────────────────
PIPEDRIVE_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
APIFY_TOKEN     = os.environ["APIFY_API_TOKEN"]
PD_BASE         = "https://slang.pipedrive.com/api/v1"
APIFY_BASE      = "https://api.apify.com/v2"

# ── Apify actor ─────────────────────────────────────────────────
ACTOR_ID = "bebity~linkedin-company-scraper"

# ── Pipedrive field keys ─────────────────────────────────────────
ORG_LINKEDIN_KEY = "82b4cd3605c175dba7512c673946b8d4b4d83427"
ICP_KEY          = "8396020706201279de40c71d6c2d5d8f2bc8fa6a"
ICP_OPTION_LT50  = 1428  # "Menos de 50 empleados"

# LinkedIn ranges que corresponden a < 50 empleados
SMALL_COMPANY_RANGES = {"self-employed", "1-10", "2-10", "11-50", "1-50"}

# ── Rate limiting ─────────────────────────────────────────────────
_req_count = 0
_win_start  = time.time()


def rate_limit():
    global _req_count, _win_start
    _req_count += 1
    if _req_count >= 80:
        elapsed = time.time() - _win_start
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        _req_count = 0
        _win_start = time.time()


def pd_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": PIPEDRIVE_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{PD_BASE}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def pd_put(endpoint, data):
    rate_limit()
    r = requests.put(
        f"{PD_BASE}/{endpoint}",
        params={"api_token": PIPEDRIVE_TOKEN},
        json=data,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ── Pipedrive: obtener todas las orgs con LinkedIn URL ────────────

def get_orgs_with_linkedin():
    orgs = []
    start = 0
    while True:
        resp = pd_get("organizations", {"start": start, "limit": 500})
        data = resp.get("data") or []
        if not data:
            break
        for o in data:
            li_url = o.get(ORG_LINKEDIN_KEY)
            if li_url and li_url.strip():
                orgs.append({
                    "id":      o["id"],
                    "name":    o.get("name", "?"),
                    "li_url":  li_url.strip(),
                    "icp_raw": o.get(ICP_KEY),
                })
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return orgs


# ── Apify: lanzar actor y esperar resultados ──────────────────────

def run_apify_actor(linkedin_urls: list[str]) -> list[dict]:
    """Lanza bebity/linkedin-company-scraper y devuelve los items del dataset."""
    payload = {
        "startUrls": [{"url": u} for u in linkedin_urls],
        "proxy": {"useApifyProxy": True},
    }
    headers = {"Authorization": f"Bearer {APIFY_TOKEN}", "Content-Type": "application/json"}

    # Iniciar run
    run_resp = requests.post(
        f"{APIFY_BASE}/acts/{ACTOR_ID}/runs",
        json=payload,
        headers=headers,
        timeout=60,
    )
    run_resp.raise_for_status()
    run_data = run_resp.json()["data"]
    run_id      = run_data["id"]
    dataset_id  = run_data["defaultDatasetId"]
    print(f"  Apify run iniciado: {run_id}")

    # Esperar hasta que termine (SUCCEEDED o FAILED)
    for attempt in range(120):  # max 20 minutos
        time.sleep(10)
        status_resp = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=headers,
            timeout=30,
        )
        status_resp.raise_for_status()
        status = status_resp.json()["data"]["status"]
        if attempt % 6 == 0:
            print(f"  Estado Apify: {status} ({attempt * 10}s)")
        if status == "SUCCEEDED":
            break
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {run_id} terminó con estado: {status}")
    else:
        raise RuntimeError("Timeout esperando Apify run.")

    # Obtener resultados
    items = []
    offset = 0
    while True:
        items_resp = requests.get(
            f"{APIFY_BASE}/datasets/{dataset_id}/items",
            params={"offset": offset, "limit": 1000, "format": "json"},
            headers=headers,
            timeout=60,
        )
        items_resp.raise_for_status()
        batch = items_resp.json()
        if not batch:
            break
        items.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000

    print(f"  Apify devolvió {len(items)} resultados.")
    return items


# ── Lógica: determinar si la empresa tiene < 50 empleados ─────────

def is_small_company(item: dict) -> bool:
    """Devuelve True si el item de Apify indica < 50 empleados."""
    # 1. staffCountRange ({"start": 1, "end": 10})
    scr = item.get("staffCountRange") or {}
    if scr:
        end = scr.get("end")
        if end is not None and end <= 50:
            return True
        if end is not None and end > 50:
            return False

    # 2. employeeCount / staffCount numérico
    for key in ("employeeCount", "staffCount", "numberOfEmployees"):
        val = item.get(key)
        if val is not None:
            try:
                if int(val) < 50:
                    return True
                if int(val) >= 50:
                    return False
            except (ValueError, TypeError):
                pass

    # 3. companySize string ("1-10", "11-50", "51-200", ...)
    size_str = (item.get("companySize") or item.get("companySizeRange") or "").strip().lower()
    if size_str:
        return size_str in SMALL_COMPANY_RANGES

    return False  # sin datos suficientes → no marcar


def normalize_li_url(url: str) -> str:
    """Normaliza la URL para comparar con el output de Apify."""
    url = url.rstrip("/").lower()
    for prefix in ("https://", "http://", "www.", "co.", "es.", "uk.", "ar.", "mx."):
        if url.startswith(prefix):
            url = url[len(prefix):]
    return url


# ── Pipedrive: actualizar ICP Non Compliance Reason ──────────────

def add_icp_lt50(org_id: int, current_icp_raw):
    """Agrega la opción 1428 al campo set ICP, conservando las demás."""
    if current_icp_raw:
        current_ids = {int(x) for x in str(current_icp_raw).split(",") if x.strip().isdigit()}
    else:
        current_ids = set()

    if ICP_OPTION_LT50 in current_ids:
        return False  # ya estaba marcado

    current_ids.add(ICP_OPTION_LT50)
    new_value = ",".join(str(i) for i in sorted(current_ids))
    resp = pd_put(f"organizations/{org_id}", {ICP_KEY: new_value})
    return resp.get("success", False)


# ── Main ──────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*60}")
    print(f"LinkedIn Size Check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    print("Obteniendo orgs con LinkedIn URL desde Pipedrive...")
    orgs = get_orgs_with_linkedin()
    print(f"Orgs con LinkedIn URL: {len(orgs)}\n")

    if not orgs:
        print("Nada que procesar.")
        return

    # Lanzar Apify con todas las URLs de una vez
    print(f"Enviando {len(orgs)} URLs a Apify ({ACTOR_ID})...")
    urls = [o["li_url"] for o in orgs]
    try:
        items = run_apify_actor(urls)
    except Exception as e:
        print(f"ERROR en Apify: {e}")
        return

    # Construir mapa url_normalizada → item
    url_to_item = {}
    for item in items:
        raw_url = item.get("linkedInUrl") or item.get("url") or item.get("companyUrl") or ""
        if raw_url:
            url_to_item[normalize_li_url(raw_url)] = item

    print(f"\nProcesando resultados...\n")
    stats = {"marked": 0, "already": 0, "large": 0, "no_data": 0, "error": 0}

    for org in orgs:
        norm = normalize_li_url(org["li_url"])
        item = url_to_item.get(norm)

        if item is None:
            print(f"  [{org['name']}] Sin resultado de Apify para {org['li_url']}")
            stats["no_data"] += 1
            continue

        small = is_small_company(item)
        size_info = (
            item.get("staffCountRange")
            or item.get("companySize")
            or item.get("employeeCount")
            or "?"
        )

        if not small:
            print(f"  [{org['name']}] {size_info} → >= 50 empleados, sin cambio.")
            stats["large"] += 1
            continue

        print(f"  [{org['name']}] {size_info} → < 50 empleados, marcando ICP...")
        try:
            updated = add_icp_lt50(org["id"], org["icp_raw"])
            if updated:
                print(f"    OK — ICP actualizado.")
                stats["marked"] += 1
            else:
                print(f"    Ya tenía la opción 1428, sin cambio.")
                stats["already"] += 1
        except Exception as e:
            print(f"    ERROR: {e}")
            stats["error"] += 1

    print(f"\n{'='*60}")
    print(
        f"Resumen: {stats['marked']} marcadas nuevas, "
        f"{stats['already']} ya marcadas, "
        f"{stats['large']} >= 50 empleados, "
        f"{stats['no_data']} sin dato de Apify, "
        f"{stats['error']} errores"
    )
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
