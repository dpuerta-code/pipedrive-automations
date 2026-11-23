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

import json
import os
import requests
import time
from datetime import datetime, date, timedelta, timezone

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

# ClickHouse — para caché de owners (detección de cambios de dueño)
CH_HOST = os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8443")
CH_USER = os.environ.get("CLICKHOUSE_USER", "s_puerta")
CH_PASS = os.environ.get("CLICKHOUSE_PASSWORD", "")
CH_ENABLED = bool(CH_PASS)  # si no hay contraseña, se desactiva el caché

# Campos de lead
PROSPECTION_DATE_KEY = "2db7aeb0017118ae0c5f9284887c0d55482bbce9"  # Prospection Date
CONTACT_DATE_KEY = "d81b6ab138f59f521821c5b29f1dc389b7a0cad4"      # Org First Contact Date (lead)

# Campos de organizacion
ORG_LAST_PROSPECTION_KEY = "2fd7273aed05f1cbab54ec64bbdb7e5dfe69fd22"  # Last Prospection Date
ORG_FIRST_CONTACT_KEY = "cd5eb85596e968a2d3cdf9a8785ba1b53982ef7a"     # First Contact Date
ORG_COUNT_FIRST_CONTACT_KEY = "e117d76508f5bdc87d55c35f3c30dacd10c6f7d9"  # Count - Org First Contact Date (enum)
ORG_COUNT_ZERO_OPTION = 1412  # "0"
ORG_COUNT_ONE_OPTION = 1413   # "1"

WINDOW_DAYS = 60        # +/- dias alrededor del Prospection Date que cuentan como "contacto valido"
LOOKBACK_DAYS = 3       # cuantos dias hacia atras de actividad/prospeccion se revisan en cada corrida
DEAL_LOOKBACK_DAYS = 2  # deals creados hace mas de N dias se ignoran en el Paso 3
ARCHIVED_LOOKBACK_DAYS = 30  # leads archivados en los ultimos N dias tambien se procesan en Paso 2
MAX_ACT_PAGES = 15      # tope de paginas al buscar actividades historicas de una org

# BACKFILL_MODE: cuando es true, Paso 2 busca en todo el historial de la org
# (no solo los ultimos 3 dias) para leads que llevan tiempo sin contacto detectado.
# Se activa via env var o desde el workflow semanal.
BACKFILL_MODE = os.environ.get("BACKFILL_MODE", "false").lower() == "true"

CONTACT_ACTIVITY_TYPES = [
    "whatsapp",
    "aircall_outbound_answered_",
    "aircall_outbound_unanswere",
    "aircall_inbound_answered_c",
    "aircall_missed_call_with_v",
    "aircall_missed_call_withou",
    "aircall_inbound_whatsapp_m",
    "aircall_outbound_whatsapp_",
    "call_manual_colombia",
]

_org_country_cache = {}


def get_org_country(org_id):
    """Retorna address_country de la org (string o None). Usa caché local."""
    if org_id not in _org_country_cache:
        try:
            resp = api_get(f"organizations/{org_id}")
            _org_country_cache[org_id] = (resp.get("data") or {}).get("address_country")
        except Exception:
            _org_country_cache[org_id] = None
    return _org_country_cache[org_id]


def is_colombia_org(org_id):
    country = get_org_country(org_id) or ""
    return "colombia" in country.lower()

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
MAX_LEADS_TEST_MODE = 5

request_count = 0
window_start = time.time()


# ── ClickHouse helpers ──────────────────────────────────────────────────────

def ch_query(sql):
    r = requests.post(
        f"https://{CH_HOST}:{CH_PORT}/",
        params={"query": sql},
        auth=(CH_USER, CH_PASS),
        timeout=60,
    )
    r.raise_for_status()
    return r.text


def ch_insert(rows):
    if not rows:
        return
    body = "\n".join(json.dumps(row) for row in rows)
    r = requests.post(
        f"https://{CH_HOST}:{CH_PORT}/",
        params={"query": "INSERT INTO bpa.lead_owner_cache FORMAT JSONEachRow"},
        auth=(CH_USER, CH_PASS),
        data=body.encode(),
        timeout=60,
    )
    r.raise_for_status()


def load_owner_cache():
    """Devuelve {lead_id_str: owner_id_int} desde ClickHouse."""
    try:
        text = ch_query("SELECT lead_id, owner_id FROM bpa.lead_owner_cache FINAL")
        cache = {}
        for line in text.strip().splitlines():
            parts = line.split("\t")
            if len(parts) == 2:
                cache[parts[0]] = int(parts[1])
        return cache
    except Exception as e:
        print(f"  [owner_cache] Error cargando caché: {e}")
        return {}


def save_owner_cache(leads):
    """Guarda/actualiza owner_id de todos los leads activos en ClickHouse."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for lead in leads:
        owner = extract_id(lead.get("owner_id"))
        if owner:
            rows.append({"lead_id": str(lead["id"]), "owner_id": int(owner), "updated_at": now})
    try:
        ch_insert(rows)
        print(f"  [owner_cache] {len(rows)} entradas guardadas en ClickHouse")
    except Exception as e:
        print(f"  [owner_cache] Error guardando caché: {e}")


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


def get_recently_archived_leads():
    """Leads archivados en los últimos ARCHIVED_LOOKBACK_DAYS días con Prospection Date y sin Org First Contact Date."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=ARCHIVED_LOOKBACK_DAYS)).isoformat()
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"archived_status": "archived", "limit": 500, "start": start})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [
        l for l in leads
        if l.get(PROSPECTION_DATE_KEY)
        and l.get("organization_id")
        and (l.get("update_time") or "")[:10] >= cutoff
    ]


def get_recently_archived_all():
    """Leads archivados en los últimos ARCHIVED_LOOKBACK_DAYS días (con o sin FCD).
    Usado en backfill para detectar cambios de owner."""
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=ARCHIVED_LOOKBACK_DAYS)).isoformat()
    leads = []
    start = 0
    while True:
        resp = api_get("leads", {"archived_status": "archived", "limit": 500, "start": start})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [
        l for l in leads
        if l.get("organization_id")
        and (l.get("update_time") or "")[:10] >= cutoff
    ]


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
    """org_id -> lista de (date_str, user_id) de contacto reciente.
    Trae user_id=0 (todos) para luego filtrar por owner de cada lead."""
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
                uid = a.get("user_id")
                if d:
                    by_org.setdefault(org_id, []).append((d[:10], uid))
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                start = pagination["next_start"]
            else:
                break

    return by_org


def get_orgs_needing_contact_date():
    """Orgs con Last Prospection Date en los últimos ARCHIVED_LOOKBACK_DAYS días
    y sin First Contact Date vigente para esa sesión."""
    cutoff = (date.today() - timedelta(days=ARCHIVED_LOOKBACK_DAYS)).isoformat()
    orgs = []
    start = 0
    while True:
        resp = api_get("organizations", {"limit": 500, "start": start})
        data = resp.get("data") or []
        if not data:
            break
        for org in data:
            last_prosp = (org.get(ORG_LAST_PROSPECTION_KEY) or "")[:10]
            if not last_prosp or last_prosp < cutoff:
                continue
            first_contact = (org.get(ORG_FIRST_CONTACT_KEY) or "")[:10]
            if first_contact:
                gap = abs((to_date(last_prosp) - to_date(first_contact)).days)
                if gap < WINDOW_DAYS:
                    continue  # FCD vigente para esta sesión, ya está bien
            orgs.append(org)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return orgs


def fetch_activities_window_by_org(start_date, end_date, person_cache):
    """org_id -> lista de (date_str, user_id) de actividades en la ventana dada.
    Trae user_id=0 (todos) para luego filtrar por owner de cada lead."""
    by_org = {}
    for t in CONTACT_ACTIVITY_TYPES:
        offset = 0
        while True:
            resp = api_get("activities", {
                "type": t, "user_id": 0, "done": 1, "limit": 500, "start": offset,
                "start_date": start_date, "end_date": end_date,
            })
            data = resp.get("data") or []
            for a in data:
                org_id = get_org_id_from_activity(a, person_cache)
                if not org_id:
                    continue
                d = a.get("marked_as_done_time") or a.get("due_date") or a.get("add_time")
                uid = a.get("user_id")
                if d:
                    by_org.setdefault(org_id, []).append((d[:10], uid))
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                offset = pagination["next_start"]
            else:
                break
    return by_org


def find_qualifying_contact_date(lead, org_activity_dates, person_cache):
    prosp = to_date(lead[PROSPECTION_DATE_KEY])
    lead_owner = extract_id(lead.get("owner_id"))
    entries = list(org_activity_dates.get(lead["organization_id"], []))

    # Solo actividades del BDR actual (owner del lead)
    if lead_owner is not None:
        candidates = [d for d, uid in entries if uid == lead_owner]
    else:
        candidates = [d for d, uid in entries]

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


def _collect_activity_dates(endpoint, params, candidates, owner_id=None):
    """Pagina sobre un endpoint de actividades y agrega fechas calificantes a candidates.
    Si owner_id está dado, solo cuenta actividades de ese usuario."""
    act_start = 0
    page = 0
    try:
        while page < MAX_ACT_PAGES:
            ar = api_get(endpoint, {**params, "done": 1, "limit": 100, "start": act_start})
            page += 1
            for a in (ar.get("data") or []):
                t = a.get("type")
                if t not in CONTACT_ACTIVITY_TYPES:
                    continue
                if owner_id is not None and a.get("user_id") != owner_id:
                    continue  # actividad de otro BDR, ignorar
                d = a.get("due_date") or (a.get("marked_as_done_time") or "")[:10]
                if d and len(d) >= 10:
                    candidates.append(d[:10])
            pag = ar.get("additional_data", {}).get("pagination", {})
            if not pag.get("more_items_in_collection"):
                break
            act_start = pag.get("next_start", act_start + 100)
    except Exception:
        pass


def fetch_org_qualifying_contact(org_id, prosp_date, person_id, person_cache, lead_id=None, owner_id=None):
    """Busca la actividad de contacto más temprana dentro de ±WINDOW_DAYS del prosp_date.
    Si owner_id está dado, solo cuenta actividades de ese usuario (el BDR actual del lead).
    Consulta tres fuentes: actividades del lead (via lead_id), actividades de la persona,
    y actividades de la org via endpoint legado.
    Usado en reemplazos atómicos (Paso 1) y en backfill completo (Paso 2 BACKFILL_MODE)."""
    candidates = []

    # Fuente 1: actividades del lead filtradas por owner
    if lead_id:
        _collect_activity_dates(
            "activities",
            {"lead_id": lead_id, "user_id": owner_id if owner_id else 0},
            candidates,
            owner_id,
        )

    # Fuente 2: actividades de la persona (cubre deal-linked y org-linked), filtradas por owner
    if person_id:
        _collect_activity_dates(f"persons/{person_id}/activities", {}, candidates, owner_id)
    else:
        _collect_activity_dates(f"organizations/{org_id}/activities", {}, candidates, owner_id)

    if person_id:
        if person_id not in person_cache:
            try:
                pr = api_get(f"persons/{person_id}")
                person_cache[person_id] = pr.get("data") or {}
            except Exception:
                person_cache[person_id] = {}
        mail_time = person_cache[person_id].get("last_outgoing_mail_time")
        if mail_time:
            candidates.append(mail_time[:10])
    qualifying = [c for c in candidates if abs((to_date(c) - prosp_date).days) <= WINDOW_DAYS]
    return min(qualifying) if qualifying else None


def reset_on_owner_change(active_leads, org_cache, owner_cache):
    """Paso 0: detecta leads cuyo owner cambió desde la última corrida.
    Limpia CONTACT_DATE_KEY del lead (si lo tenía) y el FCD de la org,
    siempre que el owner haya cambiado — sin importar el gap de 60 días.
    Un cambio de owner siempre inicia una nueva sesión de prospección.
    """
    resets = 0
    seen_orgs = set()
    for lead in active_leads:
        lead_id = str(lead["id"])
        current_owner = extract_id(lead.get("owner_id"))
        cached_owner = owner_cache.get(lead_id)
        if cached_owner is None or cached_owner == current_owner:
            continue  # primera vez o sin cambio

        org_id = lead["organization_id"]
        title = lead.get("title", "?")
        had_fcd = bool(lead.get(CONTACT_DATE_KEY))
        print(f"  Owner cambió en lead '{title}' ({lead_id}): {cached_owner} → {current_owner}"
              + (" (tenía FCD)" if had_fcd else " (sin FCD)"))

        if not TEST_MODE:
            # Limpiar FCD del lead si lo tenía
            if had_fcd:
                try:
                    api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: None})
                except Exception as e:
                    print(f"    ERROR limpiando FCD del lead: {e}")
                    continue

            # Limpiar FCD de la org siempre que cambie el owner (aunque gap < 60d)
            if org_id not in seen_orgs:
                seen_orgs.add(org_id)
                if org_id not in org_cache:
                    try:
                        resp = api_get(f"organizations/{org_id}")
                        org_cache[org_id] = resp.get("data") or {}
                    except Exception:
                        pass
                if org_cache.get(org_id, {}).get(ORG_FIRST_CONTACT_KEY):
                    try:
                        api_put(f"organizations/{org_id}", {
                            ORG_FIRST_CONTACT_KEY: None,
                            ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ZERO_OPTION,
                        })
                        org_cache.setdefault(org_id, {})[ORG_FIRST_CONTACT_KEY] = None
                        print(f"    FCD de org {org_id} limpiado por cambio de owner")
                    except Exception as e:
                        print(f"    ERROR limpiando FCD de org {org_id}: {e}")
        else:
            print(f"    [TEST] Limpiaría FCD del lead{' y' if had_fcd else ' (sin FCD) y'} org {org_id}")

        resets += 1

    return resets


def clear_stale_org_contact_dates(recent_leads, org_cache, person_cache):
    """Para orgs re-prospectadas (gap ≥60d con el FCD actual), intenta un reemplazo
    atómico: busca la nueva actividad qualifying ANTES de borrar el campo. Solo vacía
    el campo si no encuentra nada todavía (evita el estado intermedio vacío)."""
    cleared = 0
    replaced = 0
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
        if not first_contact or (to_date(prosp) - to_date(first_contact)).days < WINDOW_DAYS:
            continue  # misma sesión o sin FCD → nada que hacer aquí

        gap_days = (to_date(prosp) - to_date(first_contact)).days
        org_name = org.get("name", str(org_id))

        # Intento de reemplazo atómico: buscar nueva fecha antes de borrar
        new_date = fetch_org_qualifying_contact(
            org_id, to_date(prosp), lead.get("person_id"), person_cache,
            lead_id=lead.get("id"), owner_id=extract_id(lead.get("owner_id")),
        )
        try:
            if new_date:
                api_put(f"organizations/{org_id}", {
                    ORG_FIRST_CONTACT_KEY: new_date,
                    ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ONE_OPTION,
                })
                org_cache[org_id][ORG_FIRST_CONTACT_KEY] = new_date
                print(f"  Org {org_id} ('{org_name}'): FCD {first_contact} → {new_date} (nueva sesion, reemplazo atomico, {gap_days}d)")
                replaced += 1
            else:
                # Sin actividad nueva todavía → borrar para que Paso 2 lo rellene cuando aparezca
                api_put(f"organizations/{org_id}", {
                    ORG_FIRST_CONTACT_KEY: None,
                    ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ZERO_OPTION,
                })
                org_cache[org_id][ORG_FIRST_CONTACT_KEY] = None
                print(f"  Org {org_id} ('{org_name}'): FCD limpiado (nueva sesion {gap_days}d, sin contacto nuevo aun)")
                cleared += 1
        except Exception as e:
            print(f"  ERROR en org {org_id}: {e}")
    return cleared, replaced


def propagate_to_org(org_id, contact_date, org_cache):
    """Actualiza First Contact Date de la org según la regla de sesión de 60 días.

    - Sin FCD existente → se llena (primera sesión).
    - FCD existente con gap >= WINDOW_DAYS → nueva sesión, se actualiza.
    - FCD existente con gap <  WINDOW_DAYS → misma sesión, se conserva el FCD original.
    """
    if org_id not in org_cache:
        try:
            resp = api_get(f"organizations/{org_id}")
            org_cache[org_id] = resp.get("data") or {}
        except Exception:
            return False
    org = org_cache[org_id]
    existing = org.get(ORG_FIRST_CONTACT_KEY)
    if existing:
        gap = (to_date(contact_date) - to_date(existing)).days
        if gap < WINDOW_DAYS:
            return False  # misma sesión, conservar FCD original
        # gap >= WINDOW_DAYS → nueva sesión, actualizar
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
    if BACKFILL_MODE:
        print("BACKFILL_MODE activo: Paso 2 buscará en todo el historial de la org")

    org_cache = {}
    person_cache = {}

    # Paso 0: detectar cambios de owner y limpiar FCD afectados
    if CH_ENABLED:
        print("\nPaso 0: cargando caché de owners desde ClickHouse...")
        owner_cache = load_owner_cache()
        print(f"  {len(owner_cache)} leads en caché")
        owner_resets = reset_on_owner_change(active_leads, org_cache, owner_cache)
        print(f"  Leads reseteados por cambio de owner: {owner_resets}")
        # Re-fetch leads para reflejar los FCDs limpiados
        if owner_resets > 0 and not TEST_MODE:
            active_leads = get_active_leads()
    else:
        print("\nPaso 0: CLICKHOUSE_PASSWORD no configurado, detección de owner change desactivada")
        owner_cache = {}
    print()

    # Paso 1: para orgs re-prospectadas recientemente (gap >=60d), reemplazar FCD atómicamente
    # o limpiar si aún no hay contacto nuevo. No limpia si el gap es <60d (misma sesión).
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent_leads = [l for l in active_leads if l[PROSPECTION_DATE_KEY][:10] >= cutoff]
    print(f"Leads con prospeccion en los ultimos {LOOKBACK_DAYS} dias: {len(recent_leads)}")
    if TEST_MODE:
        recent_leads = recent_leads[:MAX_LEADS_TEST_MODE]
        print(f"TEST MODE: revisando limpieza solo en los primeros {len(recent_leads)}.")
    cleared, replaced = clear_stale_org_contact_dates(recent_leads, org_cache, person_cache)
    print(f"Orgs con FCD reemplazado atomicamente: {replaced}, limpiado (sin contacto aun): {cleared}\n")

    # Paso 1.5: sincronizar FCD de org para leads que YA tienen contacto confirmado.
    # Cubre Causa #3: org atascada con FCD viejo porque no nació un lead nuevo que
    # disparara la limpieza, pero el lead sí tiene un contacto genuino de sesión nueva.
    contacted_leads = [l for l in active_leads if l.get(CONTACT_DATE_KEY)]
    # Por org: quedarse con el lead que tenga el contacto más reciente
    org_to_contacted = {}
    for l in contacted_leads:
        oid = l["organization_id"]
        cdate = str(l.get(CONTACT_DATE_KEY) or "")[:10]
        existing_l = org_to_contacted.get(oid)
        if not existing_l or cdate > str(existing_l.get(CONTACT_DATE_KEY) or "")[:10]:
            org_to_contacted[oid] = l
    # Solo contactos dentro de la última ventana de 60 días (evita reprocesar todo el histórico)
    contact_cutoff = (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()
    org_to_contacted = {
        oid: l for oid, l in org_to_contacted.items()
        if str(l.get(CONTACT_DATE_KEY) or "")[:10] >= contact_cutoff
    }
    print(f"Leads con contacto en últimos {WINDOW_DAYS} días para sincronizar org: {len(org_to_contacted)}")
    new_sessions_propagated = 0
    for org_id, lead in org_to_contacted.items():
        contact_date = str(lead[CONTACT_DATE_KEY])[:10]
        if propagate_to_org(org_id, contact_date, org_cache):
            new_sessions_propagated += 1
            org_name = (org_cache.get(org_id) or {}).get("name", str(org_id))
            print(f"  Org '{org_name}' ({org_id}): FCD → {contact_date} (sesion nueva detectada en lead existente)")
    print(f"Orgs actualizadas por nueva sesión en lead existente: {new_sessions_propagated}\n")

    # Paso 2: llenar Org First Contact Date en leads pendientes
    archived_pending = [l for l in get_recently_archived_leads() if not l.get(CONTACT_DATE_KEY)]
    archived_pending_ids = {l["id"] for l in archived_pending}
    pending = [l for l in active_leads if not l.get(CONTACT_DATE_KEY)] + archived_pending
    print(f"Leads pendientes de Org First Contact Date: {len(pending) - len(archived_pending)} activos + {len(archived_pending)} archivados recientes")

    stats = {"updated": 0, "no_match": 0, "error": 0, "propagated_to_org": 0}

    if BACKFILL_MODE:
        # Lógica org-first: arranca de las organizaciones con Last Prospection Date reciente
        print(f"BACKFILL_MODE: buscando orgs con Last Prospection Date (últimos {ARCHIVED_LOOKBACK_DAYS} días)...")
        target_orgs = get_orgs_needing_contact_date()
        target_org_ids = {o["id"] for o in target_orgs}
        print(f"Orgs con prospección reciente sin FCD vigente: {len(target_orgs)}")

        # Detectar leads con FCD de un BDR anterior (owner cambió pero era lead nuevo → no estaba en owner_cache)
        # Si el lead activo/archivado tiene FCD pero su owner difiere del de los leads archivados recientes
        # de la misma org, el FCD puede ser del ciclo anterior → hay que resetearlo.
        print("Verificando FCDs de owner anterior en leads activos y archivados recientes...")
        recently_archived_all = get_recently_archived_all()

        # Mapa org_id -> owners de leads archivados recientes
        archived_owners_by_org = {}
        for l in recently_archived_all:
            oid = l.get("organization_id")
            if oid:
                archived_owners_by_org.setdefault(oid, set()).add(extract_id(l.get("owner_id")))

        # Mapa org_id -> owner actual (activo primero, luego archivados pendientes)
        org_to_owner = {}
        for l in active_leads:
            oid = l.get("organization_id")
            if oid:
                owner = extract_id(l.get("owner_id"))
                if owner:
                    org_to_owner[oid] = owner
        for l in sorted(archived_pending, key=lambda x: x.get("update_time", ""), reverse=True):
            oid = l.get("organization_id")
            if oid and oid not in org_to_owner:
                owner = extract_id(l.get("owner_id"))
                if owner:
                    org_to_owner[oid] = owner

        stale_fcd_orgs = set()
        leads_to_check = [(l, False) for l in active_leads if l.get(CONTACT_DATE_KEY)]
        leads_to_check += [(l, True) for l in recently_archived_all if l.get(CONTACT_DATE_KEY)]

        for lead, is_arch in leads_to_check:
            org_id = lead.get("organization_id")
            if not org_id:
                continue
            active_owner = extract_id(lead.get("owner_id"))
            archived_owners = archived_owners_by_org.get(org_id, set())
            if not archived_owners or active_owner in archived_owners:
                continue  # sin cambio de owner
            # Owner actual no estaba entre los owners del ciclo archivado → FCD potencialmente stale
            lead_id = lead["id"]
            lead_title = lead.get("title", "?")
            stale_fcd_orgs.add(org_id)
            if TEST_MODE:
                arch_tag = " (archivado)" if is_arch else ""
                print(f"  [TEST] Lead '{lead_title}'{arch_tag}: limpiaría FCD stale (owner {archived_owners} → {active_owner})")
            else:
                try:
                    if is_arch:
                        api_patch(f"leads/{lead_id}", {"is_archived": False})
                    api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: None})
                    if is_arch:
                        api_patch(f"leads/{lead_id}", {"is_archived": True})
                    arch_tag = " (archivado)" if is_arch else ""
                    print(f"  FCD limpiado en lead '{lead_title}'{arch_tag} ({lead_id}): owner cambió")
                except Exception as e:
                    print(f"  ERROR limpiando FCD de lead {lead_id}: {e}")

        # Las orgs con FCD stale deben entrar al backfill (limpiar FCD de la org y agregar a target)
        for org_id in stale_fcd_orgs:
            if org_id in target_org_ids:
                continue
            if org_id not in org_cache:
                try:
                    resp_org = api_get(f"organizations/{org_id}")
                    org_cache[org_id] = resp_org.get("data") or {}
                except Exception:
                    continue
            org = org_cache[org_id]
            if not org.get(ORG_LAST_PROSPECTION_KEY):
                continue
            if TEST_MODE:
                print(f"  [TEST] Limpiaría FCD de org {org_id} ('{org.get('name')}') y la incluiría en backfill")
            else:
                try:
                    api_put(f"organizations/{org_id}", {
                        ORG_FIRST_CONTACT_KEY: None,
                        ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ZERO_OPTION,
                    })
                    org_cache[org_id][ORG_FIRST_CONTACT_KEY] = None
                    print(f"  FCD de org {org_id} ('{org.get('name')}') limpiado por cambio de owner")
                except Exception as e:
                    print(f"  ERROR limpiando FCD de org {org_id}: {e}")
            target_orgs.append(org)
            target_org_ids.add(org_id)

        if stale_fcd_orgs:
            print(f"Orgs con FCD stale (owner cambió): {len(stale_fcd_orgs)}")
            # Re-fetch para reflejar los FCDs limpiados
            if not TEST_MODE:
                active_leads = get_active_leads()
                archived_pending = [l for l in get_recently_archived_leads() if not l.get(CONTACT_DATE_KEY)]
                archived_pending_ids = {l["id"] for l in archived_pending}
                pending = [l for l in active_leads if not l.get(CONTACT_DATE_KEY)] + archived_pending
                print(f"Leads pendientes (actualizado): {len(pending) - len(archived_pending)} activos + {len(archived_pending)} archivados\n")

        if TEST_MODE and target_orgs:
            target_orgs = target_orgs[:MAX_LEADS_TEST_MODE]
            print(f"TEST MODE: procesando solo las primeras {len(target_orgs)} orgs.\n")

        org_backfill = {}  # se llena a continuación; definido aquí para que la segunda pasada lo vea siempre
        if not target_orgs:
            print("Nada que procesar en backfill.")
        else:
            # Construir mapa org_id -> leads sin FCD (activos + archivados recientes)
            leads_by_org = {}
            for l in pending:
                oid = l.get("organization_id")
                if oid:
                    leads_by_org.setdefault(oid, []).append(l)

            # Un solo barrido de actividades para el período completo
            total_days = ARCHIVED_LOOKBACK_DAYS + WINDOW_DAYS
            act_start_str = (date.today() - timedelta(days=total_days)).isoformat()
            act_end_str = date.today().isoformat()
            print(f"Buscando actividades ({act_start_str} → {act_end_str}, {total_days} días)...")
            backfill_dates = fetch_activities_window_by_org(act_start_str, act_end_str, person_cache)
            print(f"Orgs con actividad en la ventana: {len(backfill_dates)}\n")

            for j, org in enumerate(target_orgs, 1):
                org_id = org["id"]
                org_name = org.get("name", str(org_id))
                last_prosp_str = (org.get(ORG_LAST_PROSPECTION_KEY) or "")[:10]
                prosp_date = to_date(last_prosp_str)
                org_cache[org_id] = org  # llenar cache con lo que ya tenemos

                # Owner actual de esta org (para filtrar actividades)
                lead_owner = org_to_owner.get(org_id)

                # Buscar fecha qualifying filtrada por owner
                all_entries = backfill_dates.get(org_id, [])
                if lead_owner is not None:
                    qualifying = [d for d, uid in all_entries
                                  if uid == lead_owner and abs((to_date(d) - prosp_date).days) <= WINDOW_DAYS]
                else:
                    qualifying = [d for d, uid in all_entries
                                  if abs((to_date(d) - prosp_date).days) <= WINDOW_DAYS]

                if not qualifying:
                    print(f"[{j}/{len(target_orgs)}] '{org_name}' ({org_id}): sin actividad qualifying"
                          + (f" (owner {lead_owner})" if lead_owner else ""))
                    stats["no_match"] += 1
                    continue

                contact_date = min(qualifying)
                print(f"[{j}/{len(target_orgs)}] '{org_name}' ({org_id}): contact_date = {contact_date}"
                      + (f" (owner {lead_owner})" if lead_owner else ""))

                # Actualizar FCD de la org
                if propagate_to_org(org_id, contact_date, org_cache):
                    stats["propagated_to_org"] += 1
                    print(f"    -> FCD org: {contact_date}")

                # Parchear leads sin FCD de esta org (activos + archivados del ciclo)
                org_leads = leads_by_org.get(org_id, [])
                if not org_leads:
                    continue
                for lead in org_leads:
                    lead_id = lead["id"]
                    lead_title = lead.get("title", "?")
                    is_archived = lead_id in archived_pending_ids
                    try:
                        fresh = api_get(f"leads/{lead_id}")
                        if fresh.get("data", {}).get(CONTACT_DATE_KEY):
                            print(f"    Lead '{lead_title}': ya tiene FCD, omitido")
                            continue
                        if is_archived:
                            api_patch(f"leads/{lead_id}", {"is_archived": False})
                        resp = api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: contact_date})
                        if is_archived:
                            api_patch(f"leads/{lead_id}", {"is_archived": True})
                        if resp.get("success"):
                            archived_tag = " (archivado)" if is_archived else ""
                            print(f"    Lead '{lead_title}'{archived_tag}: FCD = {contact_date}")
                            stats["updated"] += 1
                        else:
                            stats["error"] += 1
                    except Exception as e:
                        print(f"    ERROR en lead {lead_id}: {e}")
                        stats["error"] += 1

        # Segunda pasada: leads pendientes cuya org ya tiene FCD vigente
        # (pueden ser leads de orgs que ya tenían FCD antes del backfill)
        already_patched_ids = {
            lead["id"]
            for data in org_backfill.values()
            for lead in data.get("leads_pending", [])
        }
        remaining_pending = [l for l in pending if l["id"] not in already_patched_ids]
        leads_to_patch = []
        for lead in remaining_pending:
            org_id = lead.get("organization_id")
            if not org_id:
                continue
            org_fcd = (org_cache.get(org_id, {}).get(ORG_FIRST_CONTACT_KEY) or "")[:10]
            if not org_fcd:
                continue
            prosp_str = (lead.get(PROSPECTION_DATE_KEY) or "")[:10]
            if not prosp_str:
                continue
            if abs((to_date(org_fcd) - to_date(prosp_str)).days) <= WINDOW_DAYS:
                leads_to_patch.append((lead, org_fcd))

        if leads_to_patch:
            print(f"\nSegunda pasada: {len(leads_to_patch)} leads pendientes con FCD de su org ya seteado...")
            for lead, contact_date in leads_to_patch:
                lead_id = lead["id"]
                lead_title = lead.get("title", "?")
                is_archived = lead_id in archived_pending_ids
                try:
                    fresh = api_get(f"leads/{lead_id}")
                    if fresh.get("data", {}).get(CONTACT_DATE_KEY):
                        continue
                    if is_archived:
                        api_patch(f"leads/{lead_id}", {"is_archived": False})
                    resp = api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: contact_date})
                    if is_archived:
                        api_patch(f"leads/{lead_id}", {"is_archived": True})
                    if resp.get("success"):
                        archived_tag = " (archivado)" if is_archived else ""
                        print(f"  Lead '{lead_title}'{archived_tag}: FCD = {contact_date} (de org)")
                        stats["updated"] += 1
                    else:
                        stats["error"] += 1
                except Exception as e:
                    print(f"  ERROR en lead {lead_id}: {e}")
                    stats["error"] += 1

        org_activity_dates = {}  # Paso 3 no aplica en backfill

    else:
        # Daily sync: actividades de los últimos LOOKBACK_DAYS días
        if TEST_MODE and pending:
            pending = pending[:MAX_LEADS_TEST_MODE]
            print(f"TEST MODE: procesando solo los primeros {len(pending)}.\n")

        print(f"Buscando actividad de contacto de los ultimos {LOOKBACK_DAYS} dias...")
        org_activity_dates = fetch_recent_activity_dates_by_org(person_cache)
        print(f"Organizaciones con actividad reciente: {len(org_activity_dates)}")
        print()

        for i, lead in enumerate(pending or [], 1):
            lead_id = lead["id"]
            title = lead.get("title", "?")
            org_id = lead["organization_id"]
            if lead_id in archived_pending_ids:
                contact_date = fetch_org_qualifying_contact(
                    org_id, to_date(lead[PROSPECTION_DATE_KEY]), lead.get("person_id"), person_cache,
                    lead_id=lead.get("id"), owner_id=extract_id(lead.get("owner_id")),
                )
            else:
                contact_date = find_qualifying_contact_date(lead, org_activity_dates, person_cache)

            if not contact_date:
                stats["no_match"] += 1
                continue

            try:
                fresh = api_get(f"leads/{lead_id}")
                if fresh.get("data", {}).get(CONTACT_DATE_KEY):
                    print(f"[{i}/{len(pending)}] '{title}': ya se lleno en paralelo, se omite.")
                    continue
                is_archived_lead = lead_id in archived_pending_ids
                if is_archived_lead:
                    api_patch(f"leads/{lead_id}", {"is_archived": False})
                resp = api_patch(f"leads/{lead_id}", {CONTACT_DATE_KEY: contact_date})
                if is_archived_lead:
                    api_patch(f"leads/{lead_id}", {"is_archived": True})
                if resp.get("success"):
                    archived_tag = " (archivado)" if is_archived_lead else ""
                    print(f"[{i}/{len(pending)}] '{title}'{archived_tag}: Org First Contact Date = {contact_date}")
                    stats["updated"] += 1
                    if propagate_to_org(org_id, contact_date, org_cache):
                        stats["propagated_to_org"] += 1
                        print(f"    -> tambien se lleno en la organizacion {org_id}")
                else:
                    stats["error"] += 1
            except Exception as e:
                print(f"[{i}/{len(pending)}] ERROR en lead {lead_id}: {e}")
                stats["error"] += 1

    # Paso 3: orgs con deal activo reciente pero sin lead activo y sin First Contact Date
    # Solo se considera el deal más reciente por org, y solo si fue creado hace <= DEAL_LOOKBACK_DAYS días.
    lead_org_ids = {l["organization_id"] for l in active_leads}
    active_deals = get_active_deals()

    # Por org: quedarse con el deal creado más recientemente
    deal_orgs_all = {}
    for d in active_deals:
        org_id = extract_id(d.get("org_id"))
        if not org_id or org_id in lead_org_ids:
            continue
        existing = deal_orgs_all.get(org_id)
        if not existing or (d.get("add_time", "") > existing.get("add_time", "")):
            deal_orgs_all[org_id] = d

    # Filtrar: solo deals creados en los últimos DEAL_LOOKBACK_DAYS días
    deal_cutoff = (date.today() - timedelta(days=DEAL_LOOKBACK_DAYS)).isoformat()
    deal_orgs = {
        org_id: d for org_id, d in deal_orgs_all.items()
        if (d.get("add_time") or "")[:10] >= deal_cutoff
    }

    print(f"\nOrgs con deal activo sin lead activo (total): {len(deal_orgs_all)}")
    print(f"Orgs con deal creado en los ultimos {DEAL_LOOKBACK_DAYS} dias: {len(deal_orgs)}")
    deal_contacted = 0

    for org_id, deal in deal_orgs.items():
        deal_id = deal.get("id")
        deal_created = (deal.get("add_time") or "")[:10]

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
            print(f"  [TEST] Deal org '{org_name}' ({org_id}): pondría First Contact Date = {contact_date} [deal {deal_id}, creado {deal_created}]")
            deal_contacted += 1
        else:
            try:
                api_put(f"organizations/{org_id}", {
                    ORG_FIRST_CONTACT_KEY: contact_date,
                    ORG_COUNT_FIRST_CONTACT_KEY: ORG_COUNT_ONE_OPTION,
                })
                org_cache[org_id][ORG_FIRST_CONTACT_KEY] = contact_date
                print(f"  Deal org '{org_name}' ({org_id}): First Contact Date = {contact_date} [deal {deal_id}, creado {deal_created}]")
                deal_contacted += 1
            except Exception as e:
                print(f"  ERROR en deal org {org_id} [deal {deal_id}]: {e}")

    print(f"Orgs con deal actualizadas: {deal_contacted}")

    # Guardar caché de owners al final
    if CH_ENABLED and not TEST_MODE:
        print("\nGuardando caché de owners en ClickHouse...")
        save_owner_cache(active_leads)

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['updated']} leads actualizados, {stats['propagated_to_org']} propagados a su org, "
          f"{new_sessions_propagated} sesiones nuevas sincronizadas en orgs, "
          f"{stats['no_match']} sin contacto en ventana, {stats['error']} errores, "
          f"{deal_contacted} orgs con deal actualizadas")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
