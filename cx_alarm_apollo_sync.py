#!/usr/bin/env python3
"""
Toma las filas nuevas del Sheet "Campanas CX - Executive Alarm System" (las
que alguien envio con el boton de la pregunta 6954 en Metabase) y las
inscribe en la secuencia de Apollo "CX - Alarma Ejecutiva (Ausencias)".

La secuencia queda inscrita pero el ENVIO real depende de que esa secuencia
este activa en Apollo (hoy esta pausada a proposito -- activarla es una
decision manual, no de este script).

Idempotencia: cada fila que se procesa se marca "Procesado" = TRUE en el
Sheet (columna que este script crea si no existe) y no se vuelve a tocar.
La dedup del lado de Apollo la maneja `run_dedupe=True` en /contacts (si el
email ya existe en Apollo, actualiza ese contacto en vez de crear uno
nuevo) y `add_contact_ids` salta sin error contactos ya activos en la
secuencia.

El Sheet se lee/escribe via Composio (reusa la conexion de Google Sheets
ya autorizada ahi), igual que contact_auto_lead.py y lead_queue_processor.py
-- evita depender de un service account de Google Cloud.

Ademas, cada corrida refleja el estado (se envio la campana, que respondio)
en bpa.cx_alarm_status (ClickHouse) por email, upsert via ReplacingMergeTree
-- la query 6954 en Metabase hace LEFT JOIN contra esa tabla para mostrar
"Envio campana" y "Que respondio" junto al boton "Enviar a Sheet". Como
ReplacingMergeTree reemplaza la fila COMPLETA por version (no por columna),
upsert_cx_alarm_status siempre relee el estado actual antes de escribir, para
no pisar un campo que esta corrida no toco.

TEST_MODE=true -> solo imprime que haria, no llama a Apollo, no escribe en
el Sheet ni en ClickHouse (default).
Cron GitHub Actions: todos los dias 10am Colombia.
"""

import json
import os
import time

import requests

COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"]
COMPOSIO_MCP_URL = "https://connect.composio.dev/mcp"

APOLLO_API_KEY = os.environ["APOLLO_API_KEY"]
APOLLO_BASE = "https://api.apollo.io/api/v1"

CH_HOST = os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com")
CH_PORT = os.environ.get("CLICKHOUSE_PORT", "8443")
CH_USER = os.environ.get("CLICKHOUSE_USER", "")
CH_PASS = os.environ.get("CLICKHOUSE_PASSWORD", "")
CH_BASE = f"https://{CH_HOST}:{CH_PORT}/"

SPREADSHEET_ID = "1dUdBalYRvhwWpYkPxlYmWVOMGYVpxI3wry7_J4Bwzx0"  # Campanas CX - Executive Alarm System
SHEET_RANGE = "A1:Z2000"  # sin nombre de tab -> aplica a la primera hoja visible
RESPUESTAS_RANGE = "Respuestas!A1:C2000"

APOLLO_SEQUENCE_ID = "6af4cc55a3e9b80018ca768c"  # CX - Alarma Ejecutiva (Ausencias)
SEND_FROM_EMAIL_ACCOUNT_ID = "6afe0fb16964350020fb6d8f"  # m.mendoza@slangapp.com

PROCESADO_COL = "Procesado"
APOLLO_ID_COL = "Apollo Contact ID"

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


def apollo_post(endpoint, json_body):
    rate_limit()
    headers = {"x-api-key": APOLLO_API_KEY, "Content-Type": "application/json"}
    r = requests.post(f"{APOLLO_BASE}/{endpoint}", json=json_body, headers=headers, timeout=30)
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
    reusando la conexion de Google ya autorizada en Composio -- evita
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


def ch_get(sql):
    r = requests.get(
        CH_BASE,
        params={"query": sql + " FORMAT JSONEachRow"},
        auth=(CH_USER, CH_PASS),
        timeout=60,
    )
    r.raise_for_status()
    return [json.loads(line) for line in r.text.strip().splitlines() if line]


def ch_execute(sql):
    r = requests.post(CH_BASE, params={"query": sql}, auth=(CH_USER, CH_PASS), timeout=60)
    r.raise_for_status()
    return r.text


def sql_escape(s):
    return (s or "").replace("'", "''").replace("\n", " ").replace("\r", " ")


def sql_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    return f"'{sql_escape(str(v))}'"


def upsert_cx_alarm_status(email, **fields):
    """Relee el estado actual de `email` en bpa.cx_alarm_status y escribe una
    fila nueva con los campos de `fields` sobrescritos (ReplacingMergeTree
    reemplaza la fila completa por version, no por columna -- por eso hay que
    rearmarla entera para no perder lo que esta corrida no toco)."""
    current = {"procesado": 0, "apollo_contact_id": None, "ultima_respuesta": None, "fecha_respuesta": None}
    rows = ch_get(f"SELECT * FROM bpa.cx_alarm_status FINAL WHERE email = '{sql_escape(email)}' LIMIT 1")
    if rows:
        current.update({k: rows[0].get(k) for k in current})
    current.update(fields)

    ch_execute(
        "INSERT INTO bpa.cx_alarm_status (email, procesado, apollo_contact_id, ultima_respuesta, fecha_respuesta, updated_at) "
        f"VALUES ('{sql_escape(email)}', {int(current['procesado'])}, "
        f"{sql_value(current['apollo_contact_id'])}, {sql_value(current['ultima_respuesta'])}, "
        f"{sql_value(current['fecha_respuesta'])}, now())"
    )


def get_respuestas():
    try:
        data = composio_execute("GOOGLESHEETS_BATCH_GET", {
            "spreadsheet_id": SPREADSHEET_ID,
            "ranges": [RESPUESTAS_RANGE],
        })
    except RuntimeError:
        return []  # la pestaña "Respuestas" aun no existe (nadie ha respondido)
    values = data["valueRanges"][0].get("values", [])
    if not values:
        return []
    header = values[0]
    records = []
    for row in values[1:]:
        row = row + [""] * (len(header) - len(row))
        records.append(dict(zip(header, row)))
    return records


def col_letter(idx0):
    """Indice de columna 0-based -> letra de columna (A, B, ..., Z, AA, ...)."""
    idx = idx0 + 1
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def get_records():
    data = composio_execute("GOOGLESHEETS_BATCH_GET", {
        "spreadsheet_id": SPREADSHEET_ID,
        "ranges": [SHEET_RANGE],
    })
    values = data["valueRanges"][0].get("values", [])
    if not values:
        return [], []
    header = values[0]
    records = []
    for row in values[1:]:
        row = row + [""] * (len(header) - len(row))
        records.append(dict(zip(header, row)))
    return header, records


def ensure_column(header, name):
    """Si `name` no esta en el header, la agrega como ultima columna (tanto
    en la hoja real como en la lista local `header`). Devuelve el indice
    0-based de la columna."""
    if name in header:
        return header.index(name)
    idx = len(header)
    if not TEST_MODE:
        composio_execute("GOOGLESHEETS_VALUES_UPDATE", {
            "spreadsheet_id": SPREADSHEET_ID,
            "range": f"{col_letter(idx)}1",
            "value_input_option": "RAW",
            "values": [[name]],
        })
    header.append(name)
    return idx


def derive_first_name(email):
    local_part = email.split("@")[0]
    for sep in [".", "_", "-", "+"]:
        local_part = local_part.split(sep)[0]
    return local_part.capitalize() if local_part else "N/D"


def find_or_create_apollo_contact(email, first_name, org_name, severidad):
    body = {
        "first_name": first_name,
        "last_name": "N/D",
        "email": email,
        "organization_name": org_name,
        "label_names": [f"CX Alarma - {severidad or 'Sin severidad'}"],
        "run_dedupe": True,
    }
    resp = apollo_post("contacts", body)
    contact = resp.get("contact") or {}
    return contact.get("id")


def add_to_sequence(contact_id):
    body = {
        "emailer_campaign_id": APOLLO_SEQUENCE_ID,
        "contact_ids": [contact_id],
        "send_email_from_email_account_id": SEND_FROM_EMAIL_ACCOUNT_ID,
    }
    return apollo_post(f"emailer_campaigns/{APOLLO_SEQUENCE_ID}/add_contact_ids", body)


def main():
    header, records = get_records()
    if not records:
        print("Sheet vacio, nada que procesar.")
        return

    procesado_idx = ensure_column(header, PROCESADO_COL)
    apollo_id_idx = ensure_column(header, APOLLO_ID_COL)

    sent = skipped = failed = 0

    for i, row in enumerate(records, start=2):  # fila 1 es el header
        if str(row.get(PROCESADO_COL, "")).strip().upper() == "TRUE":
            skipped += 1
            continue

        email = (row.get("email") or "").strip()
        if not email:
            continue

        first_name = derive_first_name(email)
        org_name = row.get("Organizacion") or ""
        severidad = row.get("severidad") or ""

        print(f"Fila {i}: {email} ({severidad}, {org_name})")

        if TEST_MODE:
            print("  [TEST MODE] crearia/actualizaria el contacto y lo inscribiria en la secuencia.")
            continue

        try:
            contact_id = find_or_create_apollo_contact(email, first_name, org_name, severidad)
            if not contact_id:
                raise RuntimeError("Apollo no devolvio contact_id")
            add_to_sequence(contact_id)

            composio_execute("GOOGLESHEETS_VALUES_UPDATE", {
                "spreadsheet_id": SPREADSHEET_ID,
                "range": f"{col_letter(procesado_idx)}{i}:{col_letter(apollo_id_idx)}{i}",
                "value_input_option": "RAW",
                "values": [["TRUE", contact_id]],
            })
            upsert_cx_alarm_status(email, procesado=1, apollo_contact_id=contact_id)
            sent += 1
            print(f"  OK -> contacto Apollo {contact_id}")
        except Exception as e:
            failed += 1
            print(f"  ERROR: {e}")

    print(f"\nListo. Enviados: {sent} | Ya procesados: {skipped} | Fallidos: {failed}")

    respuestas = get_respuestas()
    synced_respuestas = 0
    for r in respuestas:
        email = (r.get("Email") or "").strip()
        if not email:
            continue
        if TEST_MODE:
            print(f"[TEST MODE] sincronizaria respuesta de {email}: {r.get('Accion')}")
            continue
        try:
            upsert_cx_alarm_status(
                email,
                ultima_respuesta=r.get("Accion") or None,
                fecha_respuesta=r.get("Fecha de respuesta") or None,
            )
            synced_respuestas += 1
        except Exception as e:
            print(f"  ERROR sincronizando respuesta de {email}: {e}")

    print(f"Respuestas sincronizadas a ClickHouse: {synced_respuestas}")


if __name__ == "__main__":
    main()
