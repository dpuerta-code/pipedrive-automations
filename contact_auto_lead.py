#!/usr/bin/env python3
"""
Crea automaticamente un Lead en Pipedrive cuando se detecta contacto real
(WhatsApp, Aircall en cualquier variante, o email realmente enviado) con
una organizacion del programa de prospeccion "Prospección Claude".

No depende de ClickHouse (GitHub Actions no tiene acceso a esa red interna):
lee la lista de organizaciones a vigilar del tab "Roster" de la hoja de cola
(actualizado manualmente desde una sesion con acceso a ClickHouse cada vez
que bpa.sheet_prospeccion_matches cambia), y detecta contacto real solo con
la API de Pipedrive, con la misma logica que org_contacted_sync.py.

La hoja de cola se lee via Composio (reusa la conexion de Google ya
autorizada ahi), sin necesitar un service account de Google Cloud.

No toca el campo "Campaing" — eso es exclusivo del click en Metabase
(ver lead_queue_processor.py).

TEST_MODE=true -> solo imprime lo que haria, no crea nada.
Cron GitHub Actions: varias veces al dia.
"""

import os
import time
from datetime import date, timedelta

import json
import requests

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"]
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"

QUEUE_SPREADSHEET_ID = "1g2MVl8H17gTtmSMKPZKypPIXRwRCIboMYZycFLH9VnY"
ROSTER_TAB = "Roster"
ROTATION_TAB = "Rotacion"

LEAD_MARKER = "Prospección Claude"

# Owner de un Lead cuando su territorio no tiene nadie asignado esa semana
# en la rotacion (hueco de la ronda), o cuando el territorio es desconocido.
FALLBACK_OWNER_ID = 22926796  # Sofia Puerta (d.puerta@slangapp.com)

# BDRs del programa de prospeccion (las mismas 7 personas que rotan por los
# territorios). El "contacto real" solo cuenta si lo hizo alguien de esta
# lista — no cualquier usuario de Pipedrive.
BDR_USER_IDS = {
    27766675,  # Lina Pérez
    25168321,  # Valentina Martin Clavijo
    25308725,  # Lizeth Castro
    25594802,  # Adriana
    24675455,  # María Alejandra Camacho
    27766664,  # Carlos Cerquera
    22793685,  # Angie Rozo
}

# Custom fields de Persona usados al crear una persona nueva.
PERSON_TITLE_FIELD_KEY = "41d5b08c11bf325cc294e741f13f41a8bb8b4e7a"
PERSON_LINKEDIN_FIELD_KEY = "70ea681b6702a4b11010edee026c1b4ebfd10f71"

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

LOOKBACK_DAYS = 3  # mismo criterio de ventana que org_contacted_sync.py

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


def api_post(endpoint, data):
    rate_limit()
    r = requests.post(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


_mcp_session_id = None
_mcp_request_id = 0


def _mcp_request(method, params=None):
    """Llamada JSON-RPC al endpoint MCP de Composio (transporte HTTP con SSE).
    Reusa el Mcp-Session-Id devuelto por 'initialize' en llamadas siguientes."""
    global _mcp_session_id, _mcp_request_id
    _mcp_request_id += 1
    headers = {
        "X-CONSUMER-API-KEY": COMPOSIO_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if _mcp_session_id:
        headers["Mcp-Session-Id"] = _mcp_session_id
    body = {"jsonrpc": "2.0", "id": _mcp_request_id, "method": method}
    if params is not None:
        body["params"] = params
    r = requests.post(COMPOSIO_MCP_URL, headers=headers, json=body, timeout=60)
    r.raise_for_status()
    if not _mcp_session_id and "mcp-session-id" in r.headers:
        _mcp_session_id = r.headers["mcp-session-id"]
    # requests adivina mal el charset de la respuesta SSE (sin charset en el
    # content-type) y corrompe acentos; se fuerza UTF-8 explicitamente.
    text = r.content.decode("utf-8")
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    return None


def _mcp_ensure_session():
    if _mcp_session_id:
        return
    _mcp_request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "pipedrive-weekly", "version": "1.0"},
    })
    _mcp_request("notifications/initialized")


def composio_execute(tool_slug, arguments, _retries=3):
    """Ejecuta una accion de Composio (Google Sheets) via el endpoint MCP,
    reusando la conexion de Google ya autorizada en Composio — evita
    depender de un service account de Google Cloud.

    El endpoint MCP de Composio a veces devuelve una respuesta vacia/sin
    'result' de forma transitoria; se reintenta unas pocas veces antes de
    fallar de verdad."""
    last_error = None
    for attempt in range(_retries):
        try:
            _mcp_ensure_session()
            resp = _mcp_request("tools/call", {
                "name": "COMPOSIO_MULTI_EXECUTE_TOOL",
                "arguments": {"tools": [{"tool_slug": tool_slug, "arguments": arguments}]},
            })
            content = resp["result"]["content"][0]["text"]
            payload = json.loads(content)
            if payload.get("error"):
                raise RuntimeError(f"Composio error ({tool_slug}): {payload['error']}")
            result = payload["data"]["results"][0]
            response = result["response"]
            if not response.get("successful"):
                raise RuntimeError(f"Composio tool {tool_slug} fallo: {response}")
            return response["data"]
        except (KeyError, TypeError, requests.RequestException) as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Composio tool {tool_slug} fallo tras {_retries} intentos: {last_error}")


def sheet_get_records(sheet_name):
    """Lee todas las filas de un tab como lista de dicts, igual que
    gspread's get_all_records()."""
    data = composio_execute("GOOGLESHEETS_BATCH_GET", {
        "spreadsheet_id": QUEUE_SPREADSHEET_ID,
        "ranges": [f"{sheet_name}!A1:Z2000"],
    })
    values = data["valueRanges"][0].get("values", [])
    if not values:
        return []
    header = values[0]
    records = []
    for row in values[1:]:
        row = row + [""] * (len(header) - len(row))
        records.append(dict(zip(header, row)))
    return records


def get_roster():
    rows = sheet_get_records(ROSTER_TAB)
    return [r for r in rows if str(r.get("org_id", "")).strip()]


def current_week_monday():
    today = date.today()
    return today - timedelta(days=today.weekday())


def load_rotation_map():
    """Lee el tab Rotacion una sola vez y arma {(semana_inicio, territorio): owner_id}.
    La rotacion semanal (quien cubre cada territorio) se mantiene manualmente en
    ese tab a partir del deck de rotacion del equipo."""
    records = sheet_get_records(ROTATION_TAB)
    rotation_map = {}
    for row in records:
        semana = str(row.get("semana_inicio", "")).strip()
        try:
            territorio = int(row.get("territorio"))
            owner_id = int(row.get("owner_id"))
        except (TypeError, ValueError):
            continue
        rotation_map[(semana, territorio)] = owner_id
    return rotation_map


def get_territory_owner(rotation_map, territorio):
    """owner_id para el territorio en la semana actual, o FALLBACK_OWNER_ID si
    el territorio esta vacio en la rotacion de esta semana (o es desconocido)."""
    try:
        territorio = int(territorio)
    except (TypeError, ValueError):
        return FALLBACK_OWNER_ID
    monday = current_week_monday().isoformat()
    return rotation_map.get((monday, territorio), FALLBACK_OWNER_ID)


def fetch_recent_activity_org_ids(person_cache):
    """org_id -> True si tuvo actividad whatsapp/aircall en la ventana."""
    start_date = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    end_date = date.today().isoformat()
    org_ids = set()
    for t in CONTACT_ACTIVITY_TYPES:
        start = 0
        while True:
            resp = api_get("activities", {
                "type": t, "user_id": 0, "limit": 500, "start": start,
                "start_date": start_date, "end_date": end_date,
            })
            data = resp.get("data") or []
            for a in data:
                activity_user = a.get("user_id")
                activity_user_id = activity_user.get("value") if isinstance(activity_user, dict) else activity_user
                if activity_user_id not in BDR_USER_IDS:
                    continue
                org_id = a.get("org_id")
                if not org_id:
                    person_id = a.get("person_id")
                    if person_id:
                        if person_id not in person_cache:
                            try:
                                pr = api_get(f"persons/{person_id}")
                                person_cache[person_id] = pr.get("data") or {}
                            except Exception:
                                person_cache[person_id] = {}
                        org = person_cache[person_id].get("org_id")
                        org_id = org.get("value") if isinstance(org, dict) else org
                if org_id:
                    org_ids.add(int(org_id))
            pagination = resp.get("additional_data", {}).get("pagination", {})
            if pagination.get("more_items_in_collection"):
                start = pagination["next_start"]
            else:
                break
    return org_ids


def fetch_recent_email_org_ids():
    """org_id -> True si alguna persona de esa org, cuyo dueno en Pipedrive es
    un BDR del programa, tiene last_outgoing_mail_time reciente. owner_id de
    la Persona es el mejor proxy disponible en la API a "quien la contacto",
    ya que el endpoint de Personas no expone el remitente real del correo."""
    cutoff = date.today() - timedelta(days=LOOKBACK_DAYS)
    org_ids = set()
    start = 0
    while True:
        resp = api_get("persons", {"limit": 500, "start": start})
        data = resp.get("data") or []
        for p in data:
            owner = p.get("owner_id")
            owner_id = owner.get("value") if isinstance(owner, dict) else owner
            if owner_id not in BDR_USER_IDS:
                continue
            mail_time = p.get("last_outgoing_mail_time")
            org = p.get("org_id")
            org_id = org.get("value") if isinstance(org, dict) else org
            if mail_time and org_id:
                try:
                    d = date(*map(int, mail_time[:10].split("-")))
                except Exception:
                    continue
                if d >= cutoff:
                    org_ids.add(int(org_id))
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return org_ids


def existing_program_lead(org_id):
    resp = api_get("leads", {"organization_id": org_id, "limit": 100})
    for lead in resp.get("data") or []:
        if LEAD_MARKER in (lead.get("title") or ""):
            return lead
    return None


def find_person_in_org_by_name(name, org_id):
    """Busca una persona con el mismo nombre DENTRO de esa organizacion.
    Chequeo principal para evitar duplicados cuando el email de la hoja
    esta desactualizado."""
    if not name:
        return None
    resp = api_get("persons/search", {"term": name, "fields": "name", "organization_id": org_id})
    items = (resp.get("data") or {}).get("items") or []
    target = name.strip().lower()
    for it in items:
        person = it.get("item") or {}
        if (person.get("name") or "").strip().lower() == target:
            return person.get("id")
    return None


def find_person_by_email(email):
    """Chequeo de respaldo (el email de la hoja puede estar desactualizado,
    por eso find_person_in_org_by_name se intenta primero)."""
    if not email:
        return None
    resp = api_get("persons/search", {"term": email, "fields": "email", "exact_match": "true"})
    items = (resp.get("data") or {}).get("items") or []
    if items:
        return items[0]["item"]["id"]
    return None


def find_existing_person(contact_nombre, email, org_id):
    return find_person_in_org_by_name(contact_nombre, org_id) or find_person_by_email(email)


def create_person(nombre, email, org_id, telefono=None, cargo=None, linkedin=None):
    """Crea la Persona con todos los campos que ya tengamos disponibles:
    email, telefono (si aplica), Title (= cargo) y LinkedIn."""
    body = {"name": nombre or email, "org_id": org_id}
    if email:
        body["email"] = [{"value": email, "primary": True}]
    if telefono:
        body["phone"] = [{"value": telefono, "primary": True}]
    if cargo:
        body[PERSON_TITLE_FIELD_KEY] = cargo
    if linkedin:
        body[PERSON_LINKEDIN_FIELD_KEY] = linkedin
    resp = api_post("persons", body)
    return resp["data"]["id"]


def create_lead(sheet_company, org_id, person_id=None, owner_id=None):
    body = {
        "title": f"{sheet_company} - {LEAD_MARKER}",
        "organization_id": org_id,
    }
    if person_id:
        body["person_id"] = person_id
    if owner_id:
        body["owner_id"] = owner_id
    resp = api_post("leads", body)
    return resp["data"]["id"]


def main():
    print(f"\n{'='*60}")
    print("Contact Auto Lead")
    if TEST_MODE:
        print("MODO TEST: solo lectura, no se crea nada en Pipedrive")
    print(f"{'='*60}\n")

    roster = get_roster()
    rotation_map = load_rotation_map()
    print(f"Roster: {len(roster)} organizaciones a vigilar.")

    person_cache = {}
    activity_org_ids = fetch_recent_activity_org_ids(person_cache)
    email_org_ids = fetch_recent_email_org_ids()
    contacted_org_ids = activity_org_ids | email_org_ids
    print(f"Orgs con contacto real en los ultimos {LOOKBACK_DAYS} dias (whatsapp/aircall/email): {len(contacted_org_ids)}")
    print(f"[DEBUG] activity_org_ids={len(activity_org_ids)} email_org_ids={len(email_org_ids)}")
    roster_ids = {int(r["org_id"]) for r in roster}
    print(f"[DEBUG] overlap roster<->activity: {sorted(roster_ids & activity_org_ids)[:20]}")
    print(f"[DEBUG] overlap roster<->email: {sorted(roster_ids & email_org_ids)[:20]}")

    created = 0
    skipped_existing = 0
    skipped_no_contact = 0
    errors = 0

    for row in roster:
        org_id = int(row["org_id"])
        sheet_company = row.get("sheet_company", "")
        org_category = row.get("org_category", "Coverage")
        contact_nombre = row.get("contact_nombre", "")
        email = row.get("email", "")
        telefono = row.get("telefono", "")
        cargo = row.get("cargo", "")
        linkedin = row.get("linkedin", "")
        owner_id = get_territory_owner(rotation_map, row.get("territorio"))

        if org_id not in contacted_org_ids:
            skipped_no_contact += 1
            continue

        try:
            if existing_program_lead(org_id):
                skipped_existing += 1
                continue

            if TEST_MODE:
                if org_category == "Coverage":
                    print(f"  [TEST] '{sheet_company}' (org {org_id}, Coverage): buscaria/crearia Persona '{contact_nombre}' <{email}>, luego crearia Lead.")
                else:
                    print(f"  [TEST] '{sheet_company}' (org {org_id}, {org_category}): crearia Lead ligado solo a organization_id.")
                created += 1
                continue

            person_id = None
            if org_category == "Coverage":
                person_id = find_existing_person(contact_nombre, email, org_id)
                if not person_id:
                    person_id = create_person(contact_nombre, email, org_id, telefono, cargo, linkedin)

            lead_id = create_lead(sheet_company, org_id, person_id, owner_id)
            print(f"  '{sheet_company}' (org {org_id}): Lead creado (id {lead_id}) por contacto real detectado.")
            created += 1
        except Exception as e:
            print(f"  ERROR en org {org_id} ('{sheet_company}'): {e}")
            errors += 1

    print(f"\n{'='*60}")
    print(f"Resumen: {created} leads creados, {skipped_existing} ya tenian lead del programa, "
          f"{skipped_no_contact} sin contacto reciente, {errors} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
