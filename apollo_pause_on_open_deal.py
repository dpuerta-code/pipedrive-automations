#!/usr/bin/env python3
"""
Pausa en Apollo todos los contactos activos en BDR Campaign (Signal) y
BDR Campaign (Automática) cuya organización en Pipedrive tenga deals abiertos.

Flujo:
  1. Obtiene todos los contactos activos en ambas secuencias via Apollo.
  2. Por cada contacto, busca su persona en Pipedrive por email para obtener org_id.
  3. Consulta si esa org tiene deals abiertos en cualquier pipeline.
  4. Si sí → pausa el contacto en Apollo.

TEST_MODE=true  → solo lectura, no pausa (default).
"""

import json
import os
import time
import requests
from datetime import datetime, timezone

PIPEDRIVE_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
PIPEDRIVE_BASE = "https://slang.pipedrive.com/api/v1"
APOLLO_API_KEY = os.environ["APOLLO_API_KEY"]
APOLLO_BASE = "https://api.apollo.io/api/v1"

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

SEQ_SIGNAL     = "6af76120a1d695000c2cc894"  # BDR Campaign (Signal)
SEQ_AUTOMATICA = "6af764474fc963000c6e4408"  # BDR Campaign (Automática)
SEQUENCES = [SEQ_SIGNAL, SEQ_AUTOMATICA]

# Mailbox requerido por Apollo para la llamada de pausa (no se usa para enviar).
SEND_FROM_EMAIL_ACCOUNT_ID = "69d56166f2c8e200192f4384"  # a.rozo@slangprogram.com


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


def pd_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": PIPEDRIVE_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{PIPEDRIVE_BASE}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def apollo_post(endpoint, json_body):
    rate_limit()
    headers = {"x-api-key": APOLLO_API_KEY, "Content-Type": "application/json"}
    r = requests.post(f"{APOLLO_BASE}/{endpoint}", json=json_body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def get_active_contacts_in_sequence(sequence_id):
    """Devuelve lista de {contact_id, email} con status=active en la secuencia."""
    active = []
    page = 1
    while True:
        body = {
            "emailer_campaign_ids": [sequence_id],
            "per_page": 100,
            "page": page,
        }
        resp = apollo_post("contacts/search", body)
        contacts = resp.get("contacts") or []
        for c in contacts:
            for cs in c.get("contact_campaign_statuses") or []:
                if cs.get("emailer_campaign_id") == sequence_id and cs.get("status") == "active":
                    active.append({"contact_id": c["id"], "email": c.get("email", ""), "sequence_id": sequence_id})
                    break
        pagination = resp.get("pagination") or {}
        if page >= pagination.get("total_pages", 1):
            break
        page += 1
    return active


def find_pipedrive_org_by_email(email):
    """Devuelve org_id del primer person que coincida con ese email, o None."""
    try:
        resp = pd_get("persons/search", {"term": email, "fields": "email", "limit": 1})
        items = (resp.get("data") or {}).get("items") or []
        if items:
            person = items[0].get("item") or {}
            org = person.get("organization") or {}
            return org.get("id")
    except Exception:
        pass
    return None


_org_open_deals_cache = {}


def org_has_open_deals(org_id):
    if org_id in _org_open_deals_cache:
        return _org_open_deals_cache[org_id]
    resp = pd_get(f"organizations/{org_id}/deals", {"status": "open", "limit": 1})
    result = len(resp.get("data") or []) > 0
    _org_open_deals_cache[org_id] = result
    return result


def pause_contact_in_sequence(contact_id, sequence_id):
    """Pausa un contacto en una secuencia de Apollo."""
    body = {
        "emailer_campaign_id": sequence_id,
        "contact_ids": [contact_id],
        "send_email_from_email_account_id": SEND_FROM_EMAIL_ACCOUNT_ID,
        "status": "paused",
    }
    return apollo_post(f"emailer_campaigns/{sequence_id}/add_contact_ids", body)


def main():
    print(f"\n{'='*60}")
    print(f"Apollo Pause on Open Deal -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se pausará nada (solo lectura)")
    print(f"{'='*60}\n")

    stats = {"paused": 0, "no_org": 0, "no_open_deal": 0, "errors": 0}
    scan_log = []  # registro completo de todos los contactos revisados

    for seq_id in SEQUENCES:
        seq_name = "Signal" if seq_id == SEQ_SIGNAL else "Automática"
        print(f"Revisando secuencia: BDR Campaign ({seq_name}) ...")
        try:
            contacts = get_active_contacts_in_sequence(seq_id)
        except Exception as e:
            print(f"  ERROR obteniendo contactos de {seq_name}: {e}")
            stats["errors"] += 1
            continue

        print(f"  Contactos activos: {len(contacts)}")

        for c in contacts:
            email = c["email"]
            contact_id = c["contact_id"]
            entry = {"email": email, "sequence": seq_name, "contact_id": contact_id,
                     "org_id": None, "open_deals": False, "action": None}

            org_id = find_pipedrive_org_by_email(email)
            if not org_id:
                stats["no_org"] += 1
                entry["action"] = "no_org"
                scan_log.append(entry)
                continue

            entry["org_id"] = org_id

            try:
                has_deals = org_has_open_deals(org_id)
            except Exception as e:
                print(f"  ERROR revisando deals de org {org_id} ({email}): {e}")
                stats["errors"] += 1
                entry["action"] = "error"
                scan_log.append(entry)
                continue

            entry["open_deals"] = has_deals

            if not has_deals:
                stats["no_open_deal"] += 1
                entry["action"] = "ok"
                scan_log.append(entry)
                continue

            if TEST_MODE:
                print(f"  [TEST] Pausaría {email} en {seq_name} (org {org_id} tiene deals abiertos)")
                stats["paused"] += 1
                entry["action"] = "would_pause"
                scan_log.append(entry)
                continue

            try:
                pause_contact_in_sequence(contact_id, seq_id)
                print(f"  PAUSADO {email} en {seq_name} (org {org_id} tiene deals abiertos)")
                stats["paused"] += 1
                entry["action"] = "paused"
                scan_log.append(entry)
            except Exception as e:
                print(f"  ERROR pausando {email}: {e}")
                stats["errors"] += 1
                entry["action"] = "error"
                scan_log.append(entry)

    if scan_log:
        log_data = {
            "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "test_mode": TEST_MODE,
            "stats": stats,
            "contacts": scan_log,
        }
        with open("apollo_pause_on_open_deal_log.json", "w") as f:
            json.dump(log_data, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Resumen: {json.dumps(stats, ensure_ascii=False)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
