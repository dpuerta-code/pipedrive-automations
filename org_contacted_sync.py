#!/usr/bin/env python3
"""
Marca "Org First Contact Date" en cada lead activo cuando detecta el primer
contacto real con esa organización, y mantiene sincronizado el equivalente
a nivel de organización ("First Contact Date").

Contacto = actividad de WhatsApp, cualquier llamada de Aircall, o un correo
realmente enviado (last_outgoing_mail_time de la persona).

Regla lead: solo se llena el campo si el contacto ocurre a menos de 60 días
(antes o después) del "Prospection Date" propio de ese lead. Una vez
lleno, nunca se vuelve a tocar (write-once).

Regla organización:
  - Si "Last Prospection Date" de la org es más reciente que su
    "First Contact Date", significa que se volvió a prospectar después
    del último contacto registrado → se limpia First Contact Date
    (nuevo ciclo, hay que volver a confirmar contacto).
  - Cuando un lead activo obtiene su propio Org First Contact Date,
    se revisa la organización: si su First Contact Date está vacío,
    se llena con ese mismo valor.

TEST_MODE=true → solo procesa hasta MAX_LEADS_TEST_MODE leads pendientes
(para verificar antes de activar en masa)
Cron GitHub Actions: lunes-viernes 10am, 1:30pm y 6pm Colombia
"""

import os
import requests
import time
from datetime import datetime, date, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

# Campos de lead
PROSPECTION_DATE_KEY = "2db7aeb0017118ae0c5f9284887c0d55482bbce9"  # Prospection Date
CONTACT_DATE_KEY = "d81b6ab138f59f521821c5b29f1dc389b7a0cad4"      # Org First Contact Date (lead)

# Campos de organizacion
ORG_LAST_PROSPECTION_KEY = "2fd7273aed05f1cbab54ec64bbdb7e5dfe69fd22"  # Last Prospection Date
ORG_FIRST_CONTACT_KEY = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"     # First Contact Date
ORG_COUNT_FIRST_CONTACT_KEY = "e117d76508f5bdc87d55c35f3c30dacd10c6f7d9"  # Count - Org First Contact Date (enum)
ORG_COUNT_ZERO_OPTION = 1412  # "0"
ORG_COUNT_ONE_OPTION = 1413   # "1"

WINDOW_DAYS = 60       # +/- dias alrededor del Prospection Date que cuentan como "contacto valido"
LOOKBACK_DAYS = 3       # cuantos dias hacia atras de actividad/prospeccion se revisan en cada corrida

CONTACT_ACTIVITY_TYPES = [
    "whatsapp",
    "aircall_outbound_answered_",
    "aircall_outbound_unanswere",
    "aircall_inbound_answered_c",
    "aircall_missed_call_with_v",
    "aircall_missed_call_withou",
    "aircall_inbound_whatsapp_m",
    "aircall_outbound_whatsapp_",
]

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


def api_get(endpoint, params=None):
    rate_limit()
    p = {"api_token": API_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{BASE_URL}/{endpoint}", params=p, timeout=30)
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


def to_date(s):
    y, m, d = map(int, s[:10].split("-"))
    return date(y, m, d)


def get_active_leads():
    """Todos los leads activos (no archivados), con o sin Org First Contact Date."""
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"limit": 500, "start": start})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [l for l in leads if l.get(PROSPECTION_DATE_KEY) and l.get("organization_id")]


def get_active_deals():
    """Deals abiertos con org vinculada."""
    deals = []
    start = 0
    while True:
        resp = api_get("deals", {"status": "open", "limit": 500, "start": start})
        data = resp.get("data") or []
        deals.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [d for d in deals if d.get("org_id")]


def extract_id(field):
    """Extrae el ID de un campo que puede ser int o dict con 'value'."""
    if isinstance(field, dict):
        return field.get("value")
    return field


def get_org_id_from_activity(activity, person_cache):
    org = activity.get("org_id")
    if org:
        return org
    person_id = activity.get("person_id")
    if not person_id:
        return None
    if person_id not in person_cache:
        try:
            resp = api_get(f"persons/{person_id}")
            person_cache[person_id] = resp.get("data") or {}
        except Exception:
            person_cache[person_id] = {}
    person = person_cache[person_id]
    org = person.get("org_id")
    return org.get("value") if isinstance(org, dict) else org


def fetch_recent_activity_dates_by_org(person_cache):
    """org_id -> lista de fechas (str YYYY-MM-DD) de contacto reciente."""
    start_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = date.today().isoformat()
    by_org = {}
    for t in CONTACT_ACTIVITY_TYPES:
        start = 0
        while True:
            resp = api_get("activities", {
                "type": t, "user_id": 0, "limit": 500, "start": start,
                "start_date": start_date, "end_date": end_date,
            })
            data = resp.get("data") or []
            for a in data:
                org_id = get_org_id_from_activity(a, person_cache)
                if not org_id:
                    continue
                d = a.get("marked_as_done_time") or a.get("add_time")
                if d:
                    by_org.setdefault(org_id, []).append(d[:10])
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                start = pagination["next_start"]
            else:
                break
    return by_org


def find_qualifying_contact_date(lead, org_activity_dates, person_cache):
    prosp = to_date(lead[PROSPECTION_DATE_KEY])
    candidates = list(org_activity_dates.get(lead["organization_id"], []))

    person_id = lead.get("person_id")
    if person_id:
        if person_id not in person_cache:
            try:
                resp = api_get(f"persons/{person_id}")
                person_cache[person_id] = resp.get("data") or {}
            except Exception:
                person_cache[person_id] = {}
        mail_time = person_cache[person_id].get("last_outgoing_mail_time")
        if mail_time:
            candidates.append(mail_time[:10])

    qualifying = [c for c in candidates if abs((to_date(c) - prosp).days) <= WINDOW_DAYS]
    return min(qualifying) if qualifying else None


def clear_stale_org_contact_dates(recent_leads, org_cache):
    """Para orgs con prospeccion reciente, si su First Contact Date quedo
    vieja (de antes de este nuevo ciclo), la limpia."""
    cleared = 0
    seen_orgs = set()
    for lead in recent_leads:
        org_id = lead["organization_id"]
        if org_id in seen_orgs:
            continue
        seen_orgs.add(org_id)

        if org_id not in org_cache:
            try:
                resp = api_get(f"organizations/{org_id}")
                org_cache[org_id] = resp.get("data") or {}
            except Exception:
                continue
        org = org_cache[org_id]

        first_contact = org.get(ORG_FIRST_CONTACT_KEY)
        prosp = lead[PROSPECTION_DATE_KEY]
        if first_contact and to_date(prosp) > to_date(first_contact):
            try:
                api_put(f"organizations/{org_id}", {
                    ORG_FIRST_CONTACT_KEY: None,
                    ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ZERO_OPTION,
                })
                org_cache[org_id][ORG_FIRST_CONTACT_KEY] = None
                print(f"  Org {org_id} ('{org.get('name')}'): First Contact Date limpiado (prospeccion nueva {prosp} > contacto viejo {first_contact})")
                cleared += 1
            except Exception as e:
                print(f"  ERROR limpiando org {org_id}: {e}")
    return cleared


def propagate_to_org(org_id, contact_date, org_cache):
    """Si la org no tiene First Contact Date, la llena con este valor."""
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
    print(f"\n{'='*60}")
    print(f"Org Contacted Sync — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if TEST_MODE:
        print(f"MODO TEST: solo se procesaran hasta {MAX_LEADS_TEST_MODE} leads pendientes")
    print(f"{'='*60}\n")

    active_leads = get_active_leads()
    print(f"Leads activos con Prospection Date: {len(active_leads)}")

    org_cache = {}

    # Paso 1: limpiar First Contact Date de orgs re-prospectadas recientemente
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent_leads = [l for l in active_leads if l[PROSPECTION_DATE_KEY][:10] >= cutoff]
    print(f"Leads con prospeccion en los ultimos {LOOKBACK_DAYS} dias: {len(recent_leads)}")
    if TEST_MODE:
        recent_leads = recent_leads[:MAX_LEADS_TEST_MODE]
        print(f"TEST MODE: revisando limpieza solo en los primeros {len(recent_leads)}.")
    cleared = clear_stale_org_contact_dates(recent_leads, org_cache)
    print(f"Orgs con First Contact Date limpiado: {cleared}\n")

    # Paso 2: llenar Org First Contact Date en leads pendientes
    pending = [l for l in active_leads if not l.get(CONTACT_DATE_KEY)]
    print(f"Leads activos pendientes de Org First Contact Date: {len(pending)}")
    if TEST_MODE and pending:
        pending = pending[:MAX_LEADS_TEST_MODE]
        print(f"TEST MODE: procesando solo los primeros {len(pending)}.\n")

    print(f"Buscando actividad de contacto de los ultimos {LOOKBACK_DAYS} dias...")
    person_cache = {}
    org_activity_dates = fetch_recent_activity_dates_by_org(person_cache)
    print(f"Organizaciones con actividad reciente: {len(org_activity_dates)}\n")

    stats = {"updated": 0, "no_match": 0, "error": 0, "propagated_to_org": 0}

    for i, lead in enumerate(pending or [], 1):
        lead_id = lead["id"]
        title = lead.get("title", "?")
        org_id = lead["organization_id"]
        contact_date = find_qualifying_contact_date(lead, org_activity_dates, person_cache)

        if not contact_date:
            stats["no_match"] += 1
            continue

        # re-chequeo justo antes de escribir, por seguridad write-once
        try:
            fresh = api_get(f"leads/{lead_id}")
            if fresh.get("data", {}).get(CONTACT_DATE_KEY):
                print(f"[{i}/{len(pending)}] '{title}': ya se lleno en paralelo, se omite.")
                continue
            resp = api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: contact_date})
            if resp.get("success"):
                print(f"[{i}/{len(pending)}] '{title}': Org First Contact Date = {contact_date}")
                stats["updated"] += 1
                if propagate_to_org(org_id, contact_date, org_cache):
                    stats["propagated_to_org"] += 1
                    print(f"    -> tambien se lleno en la organizacion {org_id}")
            else:
                stats["error"] += 1
        except Exception as e:
            print(f"[{i}/{len(pending)}] ERROR en lead {lead_id}: {e}")
            stats["error"] += 1

    # Paso 3: orgs con deal activo pero sin lead activo y sin First Contact Date
    lead_org_ids = {l["organization_id"] for l in active_leads}
    active_deals = get_active_deals()

    deal_orgs = {}
    for d in active_deals:
        org_id = extract_id(d.get("org_id"))
        if org_id and org_id not in lead_org_ids:
            deal_orgs.setdefault(org_id, d)

    print(f"\nOrgs con deal activo sin lead activo: {len(deal_orgs)}")
    deal_contacted = 0

    for org_id, deal in deal_orgs.items():
        # Cargar org si no está en cache
        if org_id not in org_cache:
            try:
                resp = api_get(f"organizations/{org_id}")
                org_cache[org_id] = resp.get("data") or {}
            except Exception:
                continue
        org = org_cache[org_id]

        # Si ya tiene First Contact Date, skip
        if org.get(ORG_FIRST_CONTACT_KEY):
            continue

        # Actividades recientes de la org
        activity_dates = list(org_activity_dates.get(org_id, []))

        # Email: last_outgoing_mail_time de la persona del deal
        person_id = extract_id(deal.get("person_id"))
        if person_id:
            if person_id not in person_cache:
                try:
                    resp = api_get(f"persons/{person_id}")
                    person_cache[person_id] = resp.get("data") or {}
                except Exception:
                    person_cache[person_id] = {}
            mail_time = person_cache[person_id].get("last_outgoing_mail_time")
            if mail_time:
                activity_dates.append(mail_time[:10])

        if not activity_dates:
            continue

        contact_date = min(activity_dates)
        org_name = org.get("name", str(org_id))

        if TEST_MODE:
            print(f"  [TEST] Deal org '{org_name}' ({org_id}): pondría First Contact Date = {contact_date}")
            deal_contacted += 1
        else:
            try:
                api_put(f"organizations/{org_id}", {
                    ORG_FIRST_CONTACT_KEY: contact_date,
                    ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ONE_OPTION,
                })
                org_cache[org_id][ORG_FIRST_CONTACT_KEY] = contact_date
                print(f"  Deal org '{org_name}' ({org_id}): First Contact Date = {contact_date}")
                deal_contacted += 1
            except Exception as e:
                print(f"  ERROR en deal org {org_id}: {e}")

    print(f"Orgs con deal actualizadas: {deal_contacted}")

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['updated']} leads actualizados, {stats['propagated_to_org']} propagados a su org, "
          f"{stats['no_match']} sin contacto en ventana, {stats['error']} errores, "
          f"{deal_contacted} orgs con deal actualizadas")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
