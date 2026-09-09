#!/usr/bin/env python3
"""
Detiene (remove) contactos activos en las secuencias Apollo de Motor BDR / Coverage cuando
ya no deberian seguir recibiendo correos.

Reglas (por email, cruzando Apollo <-> Pipedrive; no se guardan campos nuevos en Pipedrive):

1. Si ya NO existe ningun Lead con Campaing=Yes para esa persona en Pipedrive (se archivo,
   se elimino o se convirtio - no se distingue cual, ver nota abajo):
     - Si la organizacion tiene al menos un deal OPEN -> se detienen TODAS las secuencias
       activas de TODA la organizacion (probable conversion a deal).
     - Si no -> se detiene solo esa persona.
2. Si el lead sigue existiendo normalmente, se revisa aparte: si la organizacion tiene un
   deal con SAL date entre el primer envio (contact_campaign_statuses.added_at) y hoy ->
   se detienen TODAS las secuencias activas de esa organizacion.

Nota tecnica (2026-09-09): el parametro archived_status de GET /leads no filtra nada en esta
cuenta (probado con 300+ leads, archived==not_archived byte a byte; un escaneo de 842 leads sin
filtro no encontro ningun is_archived=true). Por eso el check de "lead ya no existe" no se basa
en ese campo, sino en la simple ausencia del lead al buscarlo. Esto tambien puede estar afectando
a lead_sal_archive.py en produccion - fuera de alcance de este script, pendiente de revision
aparte por el usuario.

Nota tecnica 2 (2026-09-09): contacts/search no soporta filtrar por emailer_campaign_id, y
ordenar por "ultima actividad" NO correlaciona con la fecha de enrolamiento -- probado en vivo,
un contacto agregado esa misma manana no aparecia en los primeros 2000 resultados. En cambio
emailer_messages/search con emailer_campaign_ids[] + status scheduled/delayed SI filtra por
secuencia de forma exacta (un mensaje pendiente = contacto activo en esa secuencia), y es lo
que usa este script para descubrir contactos activos.

TEST_MODE=true -> solo lectura, no llama a remove_or_stop_contact_ids (default).
Cron GitHub Actions: TODOS los dias.
"""

import json
import os
import time
import requests
from datetime import datetime, timezone, date

PIPEDRIVE_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
PIPEDRIVE_BASE = "https://slang.pipedrive.com/api/v1"
APOLLO_API_KEY = os.environ["APOLLO_API_KEY"]
APOLLO_BASE = "https://api.apollo.io/api/v1"

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

# Solo se evaluan contactos agregados a la secuencia el (o despues del) dia en que este
# sistema (Campaing=Yes -> apollo_campaign_launch.py) entro en operacion. Contactos mas
# viejos vienen de flujos previos (Motor BDR manual/skill) que nunca pasaron por un Lead
# con Campaing=Yes, asi que el check de "lead no encontrado" siempre los marca como falso
# positivo -- probado en vivo el 2026-09-09: 66/67 contactos activos eran de un batch del
# 2026-08-25, ninguno ligado a un lead Campaing=Yes.
GUARD_SINCE_DATE = os.environ.get("GUARD_SINCE_DATE", "2026-09-09")

CAMPAING_KEY = "cba00ea5c8cac481d5c79d3d0d45c831d1891b47"
SAL_DATE_KEY = "8a4d1715b308943f49d7e5b270a7ea81d6f356b2"

TARGET_SEQUENCES = {
    "6a53f346a203ac0013560c86": "Motor BDR - SL·NEW",
    "6a53f362a203ac000ce374f3": "Motor BDR - SL·REACT",
    "69cd6e73276e4f00111247a4": "Coverage MX",
    "69d518168f9bb3002149790d": "Coverage CL",
    "69d3ae9434d0330021a63fbc": "Coverage Colombia",
    "69d6ae5d10d96f0011bc571b": "Coverage LATAM",
}

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


def apollo_get(endpoint, params):
    rate_limit()
    headers = {"x-api-key": APOLLO_API_KEY}
    r = requests.get(f"{APOLLO_BASE}/{endpoint}", params=params, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def apollo_post(endpoint, json_body):
    rate_limit()
    headers = {"x-api-key": APOLLO_API_KEY, "Content-Type": "application/json"}
    r = requests.post(f"{APOLLO_BASE}/{endpoint}", json=json_body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def get_scheduled_contact_ids(sequence_id):
    """
    contacts/search no soporta filtrar por secuencia de forma confiable (ordenar por
    'ultima actividad' no correlaciona con la fecha de enrolamiento -- probado en vivo:
    un contacto agregado esa misma manana no aparecia ni en los primeros 2000 resultados).
    emailer_messages/search con emailer_campaign_ids[] SI filtra por secuencia de forma
    exacta -- un mensaje en estado scheduled/delayed implica que el contacto sigue activo
    en esa secuencia.
    """
    contact_ids = set()
    page = 1
    while True:
        resp = apollo_get("emailer_messages/search", {
            "emailer_campaign_ids[]": sequence_id,
            "emailer_message_stats[]": ["scheduled", "delayed"],
            "per_page": 100,
            "page": page,
        })
        for m in resp.get("emailer_messages") or []:
            cid = m.get("contact_id")
            if cid:
                contact_ids.add(cid)
        pagination = resp.get("pagination") or {}
        if page >= (pagination.get("total_pages") or 1):
            break
        page += 1
    return contact_ids


def get_contact(contact_id):
    return apollo_get(f"contacts/{contact_id}", {})


def get_active_apollo_contacts():
    """
    Devuelve {email: {sequence_id: added_at}} para contactos con un mensaje
    scheduled/delayed pendiente en alguna de las TARGET_SEQUENCES (added_at >=
    GUARD_SINCE_DATE, ver nota arriba de por que se excluyen contactos mas viejos).
    """
    all_contact_ids = set()
    for seq_id in TARGET_SEQUENCES:
        all_contact_ids |= get_scheduled_contact_ids(seq_id)

    active = {}
    for contact_id in all_contact_ids:
        try:
            c = get_contact(contact_id).get("contact") or {}
        except Exception as e:
            print(f"  ERROR obteniendo contact {contact_id}: {e}")
            continue
        email = c.get("email")
        if not email:
            continue
        for st in c.get("contact_campaign_statuses") or []:
            if st.get("status") != "active":
                continue
            seq_id = st.get("emailer_campaign_id")
            if seq_id not in TARGET_SEQUENCES:
                continue
            added_at = st.get("added_at")
            if not added_at or added_at[:10] < GUARD_SINCE_DATE:
                continue  # contacto de un flujo anterior, fuera del alcance de este guard
            active.setdefault(email, {})[seq_id] = added_at
    return active


def find_campaing_lead_for_email(email):
    """Busca una persona por email y, si existe, su(s) lead(s) con Campaing=Yes.
    Devuelve (org_id, org_name) si encuentra un lead Campaing=Yes vigente, o None si no."""
    resp = pd_get("persons/search", {"term": email, "fields": "email", "exact_match": "true"})
    items = (resp.get("data") or {}).get("items") or []
    if not items:
        return None
    person = items[0]["item"]
    person_id = person.get("id")
    org = person.get("organization") or {}
    org_id = org.get("id")
    org_name = org.get("name", "?")

    resp = pd_get("leads", {"person_id": person_id, "limit": 200})
    leads = resp.get("data") or []
    for lead in leads:
        if lead.get(CAMPAING_KEY) == 1453:
            return org_id, org_name
    return None


def org_has_open_deal(org_id):
    resp = pd_get(f"organizations/{org_id}/deals", {"status": "open", "limit": 1})
    return len(resp.get("data") or []) > 0


def org_deals_with_sal_date_in_window(org_id, start_date, end_date):
    resp = pd_get(f"organizations/{org_id}/deals", {"status": "all_not_deleted", "limit": 500})
    deals = resp.get("data") or []
    hits = []
    for d in deals:
        sal = d.get(SAL_DATE_KEY)
        if sal and start_date <= str(sal)[:10] <= end_date:
            hits.append(d)
    return hits


def stop_contact(email, sequence_ids, reason, backup, stats):
    if not sequence_ids:
        return
    print(f"  STOP {email} en {sequence_ids} -- motivo: {reason}")
    backup.append({"email": email, "sequence_ids": list(sequence_ids), "reason": reason})
    if TEST_MODE:
        stats["would_stop"] += 1
        return
    try:
        # remove_or_stop_contact_ids trabaja por contact_id, no por email; se resuelve
        # el contact_id via contacts/search (mismo email) antes del remove real.
        resp = apollo_get("contacts/search", {"q_keywords": email, "per_page": 1})
        contacts = resp.get("contacts") or []
        if not contacts:
            print(f"    ERROR: no se encontro contact_id de Apollo para {email}")
            stats["errors"] += 1
            return
        contact_id = contacts[0]["id"]
        apollo_post("emailer_campaigns/remove_or_stop_contact_ids", {
            "emailer_campaign_ids": list(sequence_ids),
            "contact_ids": [contact_id],
            "mode": "remove",
        })
        stats["stopped"] += 1
    except Exception as e:
        print(f"    ERROR deteniendo {email}: {e}")
        stats["errors"] += 1


def main():
    today = datetime.now(timezone.utc).date().isoformat()
    print(f"\n{'='*60}")
    print(f"Apollo Campaign Guard -- {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se llamara al remove real")
    print(f"{'='*60}\n")

    active = get_active_apollo_contacts()
    print(f"Contactos activos encontrados en las 6 secuencias: {len(active)}\n")

    stats = {"stopped": 0, "would_stop": 0, "no_action": 0, "errors": 0}
    backup = []

    # Cache por organizacion para no repetir llamadas
    org_open_deal_cache = {}
    org_sal_hit_cache = {}
    # Agrupar contactos activos por organizacion para poder detener "toda la org" de una
    org_active_contacts = {}  # org_id -> {email: set(sequence_ids)}

    pending_sal_check = []  # (email, org_id, org_name, sequence_ids, min_added_at)

    for email, seqs in active.items():
        try:
            found = find_campaing_lead_for_email(email)
        except Exception as e:
            print(f"  ERROR buscando lead para {email}: {e}")
            stats["errors"] += 1
            continue

        if found is None:
            # Lead ya no existe (archivado/eliminado/convertido) -- ver si hay deal open
            # en la organizacion. Sin org conocida (persona no encontrada del todo), no se
            # puede evaluar el check de org; se detiene solo la persona por seguridad.
            resp = pd_get("persons/search", {"term": email, "fields": "email", "exact_match": "true"})
            items = (resp.get("data") or {}).get("items") or []
            org_id = None
            if items:
                org_id = (items[0]["item"].get("organization") or {}).get("id")

            if org_id:
                if org_id not in org_open_deal_cache:
                    org_open_deal_cache[org_id] = org_has_open_deal(org_id)
                if org_open_deal_cache[org_id]:
                    org_active_contacts.setdefault(org_id, {})[email] = set(seqs.keys())
                    continue  # se resuelve mas abajo, a nivel de toda la org
            stop_contact(email, seqs.keys(), "lead_not_found_no_open_deal", backup, stats)
            continue

        org_id, org_name = found
        min_added = min((v for v in seqs.values() if v), default=None)
        pending_sal_check.append((email, org_id, org_name, set(seqs.keys()), min_added))

    # Organizaciones con lead ausente + deal open -> detener TODA la organizacion
    for org_id, contacts in org_active_contacts.items():
        all_seqs = set()
        for s in contacts.values():
            all_seqs |= s
        for email in contacts:
            stop_contact(email, all_seqs, "lead_not_found_org_has_open_deal", backup, stats)

    # Check SAL date a nivel organizacion, para los que siguen con lead vigente
    by_org = {}
    for email, org_id, org_name, seqs, min_added in pending_sal_check:
        by_org.setdefault(org_id, {"name": org_name, "contacts": {}})
        by_org[org_id]["contacts"][email] = seqs
        by_org[org_id]["min_added"] = min(
            [d for d in [by_org[org_id].get("min_added"), min_added] if d], default=None
        )

    for org_id, info in by_org.items():
        window_start_date = (info.get("min_added") or today)[:10]
        try:
            hits = org_deals_with_sal_date_in_window(org_id, window_start_date, today)
        except Exception as e:
            print(f"  ERROR revisando deals de org {org_id}: {e}")
            stats["errors"] += 1
            continue
        if hits:
            all_seqs = set()
            for s in info["contacts"].values():
                all_seqs |= s
            for email in info["contacts"]:
                stop_contact(email, all_seqs, "sal_date_in_window", backup, stats)
        else:
            stats["no_action"] += len(info["contacts"])

    if backup:
        with open("apollo_campaign_guard_backup.json", "w") as f:
            json.dump(backup, f, indent=2, ensure_ascii=False)

    print(f"\n{'='*60}")
    print(f"Resumen: {json.dumps(stats, ensure_ascii=False)}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
