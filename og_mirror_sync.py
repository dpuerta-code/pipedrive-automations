#!/usr/bin/env python3
"""
Sincroniza el campo "Organization Category" desde la organización
hacia el objeto persona y el objeto lead, para todos los leads
del filtro OG mirror (ID 607).

TEST_MODE=true → solo procesa el primer lead (para verificar antes de activar en masa)
Cron GitHub Actions: lunes–viernes 7pm Colombia (00:00 UTC)
"""

import os
import requests
import time
from datetime import datetime

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"
FILTER_ID = 607  # OG mirror

# Custom field hashes
ORG_CAT_KEY    = "16888f0496b3f7a0cbeedbf8a0704cbad8a81c42"
PERSON_CAT_KEY = "0dc5fa3d447d6bd47c4cbf9a076a70326db0077a"
LEAD_CAT_KEY   = "fafdb80a27427f23c8f72675767e616461f056cb"

# Field definition IDs (para leer opciones del enum en runtime)
ORG_CAT_FIELD_ID    = 41
PERSON_CAT_FIELD_ID = 54
LEAD_CAT_FIELD_ID   = 190

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


def build_label_to_id_map(field_endpoint, field_id):
    """Devuelve {label_lower: option_id} para un campo enum."""
    resp = api_get(f"{field_endpoint}/{field_id}")
    options = resp.get("data", {}).get("options", [])
    return {o["label"].strip().lower(): o["id"] for o in options}


def get_leads_from_filter():
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"filter_id": FILTER_ID, "limit": 500, "start": start})
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


def get_org_category_label(org_id, org_label_map):
    """Devuelve el label del Organization Category de la org, o None si no tiene."""
    try:
        resp = api_get(f"organizations/{org_id}")
        org = resp.get("data", {})
        option_id = org.get(ORG_CAT_KEY)
        if option_id is None:
            return None
        # org_label_map es {id: label}
        return org_label_map.get(int(option_id))
    except Exception as e:
        print(f"  Error obteniendo org {org_id}: {e}")
        return None


def main():
    print(f"\n{'='*60}")
    print(f"OG Mirror Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if TEST_MODE:
        print("MODO TEST: solo se procesará el primer lead")
    print(f"{'='*60}\n")

    # Construir mapas label→id para persona y lead
    print("Cargando opciones de campos...")
    org_options_resp = api_get(f"organizationFields/{ORG_CAT_FIELD_ID}")
    org_id_to_label = {
        o["id"]: o["label"].strip()
        for o in org_options_resp.get("data", {}).get("options", [])
    }

    person_label_to_id = build_label_to_id_map("personFields", PERSON_CAT_FIELD_ID)
    lead_label_to_id   = build_label_to_id_map("dealFields", LEAD_CAT_FIELD_ID)

    print(f"Opciones org: {org_id_to_label}")
    print(f"Opciones person: {person_label_to_id}")
    print(f"Opciones lead: {lead_label_to_id}\n")

    # Obtener leads del filtro
    leads = get_leads_from_filter()
    if not leads:
        print("El filtro OG mirror no devolvió leads. Nada que hacer.")
        return

    print(f"Leads en filtro: {len(leads)}")
    if TEST_MODE:
        leads = leads[:1]
        print("TEST MODE: procesando solo el primero.\n")

    stats = {"updated": 0, "skipped": 0, "error": 0}

    for i, lead in enumerate(leads):
        lead_id    = lead["id"]
        lead_title = lead.get("title", "?")
        org_id     = lead.get("organization_id")
        person_id  = lead.get("person_id")

        print(f"[{i+1}/{len(leads)}] Lead '{lead_title}' (org={org_id}, person={person_id})")

        if not org_id:
            print("  SKIP: sin organización vinculada.")
            stats["skipped"] += 1
            continue

        label = get_org_category_label(org_id, org_id_to_label)
        if not label:
            print(f"  SKIP: la org {org_id} no tiene Organization Category.")
            stats["skipped"] += 1
            continue

        label_lower = label.strip().lower()
        person_option_id = person_label_to_id.get(label_lower)
        lead_option_id   = lead_label_to_id.get(label_lower)

        if not person_option_id or not lead_option_id:
            print(f"  ERROR: no se encontró la opción '{label}' en person o lead.")
            stats["error"] += 1
            continue

        print(f"  Categoría org: '{label}' → person_id={person_option_id}, lead_id={lead_option_id}")

        # Actualizar persona
        ok_person = False
        if person_id:
            try:
                resp = api_put(f"persons/{person_id}", {PERSON_CAT_KEY: person_option_id})
                ok_person = resp.get("success", False)
                print(f"  Person {person_id}: {'OK' if ok_person else 'FAIL'}")
            except Exception as e:
                print(f"  Error actualizando person {person_id}: {e}")
        else:
            print("  Sin persona vinculada, se omite actualización de person.")
            ok_person = True

        # Actualizar lead
        ok_lead = False
        try:
            resp = api_patch(f"leads/{lead_id}", {LEAD_CAT_KEY: lead_option_id})
            ok_lead = resp.get("success", False)
            print(f"  Lead {lead_id}: {'OK' if ok_lead else 'FAIL'}")
        except Exception as e:
            print(f"  Error actualizando lead {lead_id}: {e}")

        if ok_person and ok_lead:
            stats["updated"] += 1
        else:
            stats["error"] += 1

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['updated']} actualizados, {stats['skipped']} sin categoría, {stats['error']} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
