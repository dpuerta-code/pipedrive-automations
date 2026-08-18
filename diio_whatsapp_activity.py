#!/usr/bin/env python3
"""
diio_whatsapp_activity.py

Detecta notas de Pipedrive creadas por Diio que contengan el texto
"detalle de la conversación escrita de Whatsapp" y crea una actividad
de tipo whatsapp marcada como hecha, asignada al rep del deal/lead.

TEST_MODE=true  → solo lectura, no ejecuta cambios.
Cron: 12pm y 7pm Colombia (17:00 y 00:00 UTC).
Duplicados: maximo 1 actividad por persona por dia. find_existing_whatsapp_activity
busca por person_id (ademas de deal_id/lead_id) porque el 99%+ de las notas de
Diio solo traen person_id -- sin esa busqueda, cada corrida del cron que volvia
a ver la misma nota dentro de la ventana de LOOKBACK_HOURS creaba una actividad
nueva en vez de encontrar la ya creada.
"""

import os
import requests
import time
from datetime import datetime, timezone, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"

# Buscar notas de las últimas N horas (cubre la ventana entre las 2 corridas diarias)
LOOKBACK_HOURS = 14

DIIO_TRIGGER = "detalle de la conversación escrita de Whatsapp"
ACTIVITY_SUBJECT = "Whatsapp Message Diio"
ACTIVITY_TYPE = "whatsapp"

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
    r = requests.post(
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


def get_notes_in_window(since_date_str, until_date_str):
    """
    Pagina todas las notas en el rango de fechas (filtro por add_time).
    Luego se filtra client-side por update_time para capturar actualizaciones.
    """
    notes = []
    start = 0
    while True:
        resp = api_get("notes", {
            "start_date": since_date_str,
            "end_date": until_date_str,
            "sort": "update_time DESC",
            "limit": 500,
            "start": start,
        })
        data = resp.get("data") or []
        if not data:
            break
        notes.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return notes


def find_existing_whatsapp_activity(deal_id, lead_id, person_id, due_date):
    """
    Busca cualquier actividad de tipo whatsapp para este deal/lead/persona en
    esta fecha. Retorna la actividad encontrada (dict) o None.
    Prioridad: si ya existe 'Whatsapp Message Diio', la retorna primero.

    NOTA: la busqueda por lead_id via /v1/activities esta ahi por compatibilidad
    pero ese endpoint generico solo devuelve actividades del dueno del token
    (bug conocido de la API), asi que no es confiable. El 99%+ de las notas de
    Diio traen person_id, y /v1/persons/{id}/activities si devuelve datos
    completos de toda la compania -- por eso esa es la busqueda que realmente
    evita los duplicados.
    """
    candidates = []

    if deal_id:
        try:
            resp = api_get(f"deals/{deal_id}/activities", {
                "done": 1,
                "start": 0,
                "limit": 200,
            })
            for act in (resp.get("data") or []):
                if act.get("type") == ACTIVITY_TYPE and act.get("due_date") == due_date:
                    candidates.append(act)
        except Exception:
            pass

    if lead_id:
        try:
            resp = api_get("activities", {
                "lead_id": lead_id,
                "user_id": 0,
                "start_date": due_date,
                "end_date": due_date,
                "limit": 200,
            })
            for act in (resp.get("data") or []):
                if act.get("type") == ACTIVITY_TYPE:
                    candidates.append(act)
        except Exception:
            pass

    if person_id:
        try:
            start = 0
            while True:
                resp = api_get(f"persons/{person_id}/activities", {"start": start, "limit": 500})
                data = resp.get("data") or []
                for act in data:
                    if act.get("type") == ACTIVITY_TYPE and act.get("due_date") == due_date:
                        candidates.append(act)
                pagination = resp.get("additional_data", {}).get("pagination", {})
                if pagination.get("more_items_in_collection"):
                    start = pagination["next_start"]
                else:
                    break
        except Exception:
            pass

    if not candidates:
        return None
    # Si ya está la versión Diio, devolverla primero (así se skippea sin tocar)
    for act in candidates:
        if act.get("subject") == ACTIVITY_SUBJECT:
            return act
    return candidates[0]


def main():
    now_utc = datetime.now(timezone.utc)
    colombia_now = now_utc - timedelta(hours=5)
    today_str = colombia_now.strftime("%Y-%m-%d")

    since_dt = colombia_now - timedelta(hours=LOOKBACK_HOURS)
    since_str = since_dt.strftime("%Y-%m-%d")
    since_iso = since_dt.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*60}")
    print(f"Diio WhatsApp Activity Sync — {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se ejecutarán cambios")
    print(f"Ventana: desde {since_iso} Colombia")
    print(f"{'='*60}\n")

    notes = get_notes_in_window(since_str, today_str)
    print(f"Notas en el rango {since_str} → {today_str}: {len(notes)}")

    # Filtrar notas con el texto Diio Y actualizadas dentro de la ventana
    diio_notes = []
    for n in notes:
        content = n.get("content") or ""
        if DIIO_TRIGGER not in content:
            continue
        # Solo notas actualizadas dentro de la ventana de LOOKBACK_HOURS
        update_time = n.get("update_time") or n.get("add_time") or ""
        try:
            note_dt = datetime.strptime(update_time[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) - timedelta(hours=5)
        except Exception:
            note_dt = colombia_now  # si no se puede parsear, incluir
        if note_dt < since_dt:
            continue
        # Necesita al menos un entity link
        if not (n.get("deal_id") or n.get("lead_id") or n.get("person_id")):
            continue
        diio_notes.append(n)

    print(f"Notas Diio en ventana con conversación WA: {len(diio_notes)}\n")

    if not diio_notes:
        print("Nada que procesar.")
        return

    created = 0
    replaced = 0
    skipped = 0
    errors = 0

    for note in diio_notes:
        note_id = note["id"]
        user_id = note.get("user_id")
        deal_id = note.get("deal_id")
        lead_id = note.get("lead_id")
        person_id = note.get("person_id")
        org_id = note.get("org_id")

        note_date = (note.get("update_time") or note.get("add_time") or today_str)[:10]
        user_name = (note.get("user") or {}).get("name", str(user_id))
        entity = f"deal={deal_id}" if deal_id else (f"lead={lead_id}" if lead_id else f"person={person_id}")

        print(f"Nota {note_id} | {entity} | rep: {user_name} | fecha: {note_date}")

        existing = find_existing_whatsapp_activity(deal_id, lead_id, person_id, note_date)

        if existing:
            existing_subject = existing.get("subject", "")
            existing_id = existing["id"]

            # Ya está procesada con el nombre correcto
            if existing_subject == ACTIVITY_SUBJECT:
                print(f"  SKIP: ya existe '{ACTIVITY_SUBJECT}' (id={existing_id}) para {entity} en {note_date}")
                skipped += 1
                continue

            # Existe una actividad whatsapp con otro nombre → reemplazar
            if TEST_MODE:
                print(f"  [TEST] Reemplazaría actividad {existing_id} '{existing_subject}' → '{ACTIVITY_SUBJECT}'")
                replaced += 1
            else:
                try:
                    resp = api_patch(f"activities/{existing_id}", {"subject": ACTIVITY_SUBJECT})
                    if resp.get("success"):
                        print(f"  Actividad {existing_id} renombrada: '{existing_subject}' → '{ACTIVITY_SUBJECT}'")
                        replaced += 1
                    else:
                        print(f"  ERROR renombrando actividad {existing_id}: {resp}")
                        errors += 1
                except Exception as e:
                    print(f"  ERROR renombrando actividad {existing_id}: {e}")
                    errors += 1
            continue

        # No existe → crear nueva
        payload = {
            "subject": ACTIVITY_SUBJECT,
            "type": ACTIVITY_TYPE,
            "done": 1,
            "due_date": note_date,
            "user_id": user_id,
        }
        if deal_id:
            payload["deal_id"] = deal_id
        if lead_id:
            payload["lead_id"] = lead_id
        if person_id:
            payload["person_id"] = person_id
        if org_id:
            payload["org_id"] = org_id

        if TEST_MODE:
            print(f"  [TEST] Crearía: subject='{ACTIVITY_SUBJECT}' | {entity} | rep={user_name} | done=1")
            created += 1
        else:
            try:
                resp = api_post("activities", payload)
                if resp.get("success"):
                    act_id = resp["data"]["id"]
                    print(f"  Actividad {act_id} creada → {ACTIVITY_SUBJECT} | {entity} | {user_name}")
                    created += 1
                else:
                    print(f"  ERROR creando actividad: {resp}")
                    errors += 1
            except Exception as e:
                print(f"  ERROR nota {note_id}: {e}")
                errors += 1

    print(f"\n{'='*60}")
    if TEST_MODE:
        print(f"[TEST] Habría creado {created}, reemplazado {replaced}, {skipped} ya existían, {errors} errores")
    else:
        print(f"Resumen: {created} creadas, {replaced} reemplazadas, {skipped} ya existían, {errors} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
