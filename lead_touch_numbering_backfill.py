#!/usr/bin/env python3
"""
Numera los "toques" (intentos de contacto) de cada lead ACTIVO (no archivado)
en el subject de sus actividades, agregando el prefijo "Toque N - ".

Dos canales, numerados por separado y por dia distinto (no por llamada
individual: varias llamadas o mensajes el mismo dia comparten el mismo
numero de toque):

  - Aircall: cualquier actividad type que empiece con "aircall_", matcheada
    directo por el lead_id que ya trae la actividad.
  - WhatsApp Diio: actividades type="whatsapp" con subject="Whatsapp Message
    Diio" (las crea diio_whatsapp_activity.py). Estas actividades NUNCA
    traen lead_id seteado (se crean solo con deal_id/person_id/org_id), asi
    que se asocian por person_id -- y SOLO si esa persona tiene exactamente
    1 lead activo (evita asignar mal si hay 2+ leads para la misma persona).
    Una vez matcheadas, se les setea el lead_id en Pipedrive para que
    queden asociadas de verdad, no solo en el calculo local.

  Mismo criterio (person_id + "exactamente 1 lead activo") se usa para
  asociarle lead_id a las NOTAS de Diio (las que contienen el texto
  "detalle de la conversacion escrita de Whatsapp"), que tienen el mismo
  hueco de asociacion que las actividades.

Idempotente: si el subject ya tiene el prefijo "Toque N - ", se le quita
antes de recalcular, asi se puede re-correr sin ir acumulando prefijos ni
perder el numero si aparecen actividades nuevas.

TEST_MODE=true -> solo calcula y muestra que haria, no escribe nada.
Sin schedule (workflow_dispatch manual) -- es un backfill, no una
automatizacion recurrente.
"""

import os
import re
import json
import time
import requests
from collections import defaultdict

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

AIRCALL_PREFIX = "aircall_"
DIIO_SUBJECT = "Whatsapp Message Diio"
DIIO_NOTE_TRIGGER = "detalle de la conversación escrita de Whatsapp"
TOQUE_PREFIX_RE = re.compile(r"^Toque \d+ - ")

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


def api_put(endpoint, data):
    rate_limit()
    r = requests.put(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_active_leads():
    leads, start = [], 0
    while True:
        resp = api_get("leads", {"start": start, "limit": 500})
        data = resp.get("data") or []
        leads.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return [l for l in leads if l.get("person_id") and l.get("organization_id")]


def get_person_activities(person_id):
    items, start = [], 0
    while True:
        resp = api_get(f"persons/{person_id}/activities", {"start": start, "limit": 500})
        data = resp.get("data") or []
        items.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return items


def get_person_diio_notes(person_id):
    resp = api_get("notes", {"person_id": person_id, "limit": 200})
    data = resp.get("data") or []
    return [n for n in data if DIIO_NOTE_TRIGGER in (n.get("content") or "")]


def build_rename(matches):
    """Dado una lista de actividades de un solo canal para un lead, arma
    (activity, nuevo_subject) por cada una que necesite cambiar, agrupando
    el numero de toque por due_date distinto."""
    out = []
    dates = sorted(set(a.get("due_date") for a in matches if a.get("due_date")))
    date_to_touch = {d: i + 1 for i, d in enumerate(dates)}
    for a in matches:
        d = a.get("due_date")
        if not d:
            continue
        touch_n = date_to_touch[d]
        old_subject = a.get("subject") or ""
        base_subject = TOQUE_PREFIX_RE.sub("", old_subject)
        new_subject = f"Toque {touch_n} - {base_subject}"
        if new_subject != old_subject:
            out.append((a, new_subject))
    return out


def main():
    print(f"\n{'='*60}")
    print("Lead Touch Numbering Backfill")
    if TEST_MODE:
        print(f"MODO TEST: solo se procesaran los primeros {MAX_LEADS_TEST_MODE} leads")
    print(f"{'='*60}\n")

    leads = get_active_leads()
    print(f"Leads activos: {len(leads)}")

    pid_to_leads = defaultdict(list)
    for l in leads:
        pid_to_leads[l["person_id"]].append(l["id"])
    single_lead_persons = {pid for pid, ls in pid_to_leads.items() if len(ls) == 1}
    print(f"Personas con exactamente 1 lead activo: {len(single_lead_persons)}")
    print(f"Personas con 2+ leads activos (se excluyen del WhatsApp/notas por ambiguedad): "
          f"{sum(1 for ls in pid_to_leads.values() if len(ls) > 1)}\n")

    if TEST_MODE:
        leads = leads[:MAX_LEADS_TEST_MODE]

    stats = {
        "activities_renamed": 0, "activities_rename_error": 0,
        "activity_lead_id_set": 0, "activity_lead_id_error": 0,
        "note_lead_id_set": 0, "note_lead_id_error": 0,
    }
    result_log = []
    activities_cache = {}

    for i, lead in enumerate(leads, 1):
        lead_id = lead["id"]
        pid = lead["person_id"]
        title = lead.get("title", "?")

        if pid not in activities_cache:
            activities_cache[pid] = get_person_activities(pid)
        acts = activities_cache[pid]

        aircall_matches = [a for a in acts if (a.get("type") or "").startswith(AIRCALL_PREFIX)
                           and a.get("lead_id") == lead_id]

        whatsapp_matches = []
        if pid in single_lead_persons:
            whatsapp_matches = [a for a in acts if a.get("type") == "whatsapp" and a.get("subject") == DIIO_SUBJECT]

            # asociar lead_id en las actividades de whatsapp que no lo tengan
            for a in whatsapp_matches:
                if a.get("lead_id") == lead_id:
                    continue
                if TEST_MODE:
                    print(f"[{i}/{len(leads)}] '{title}': [TEST] asignaria lead_id a actividad whatsapp {a['id']}")
                    stats["activity_lead_id_set"] += 1
                else:
                    try:
                        resp = api_put(f"activities/{a['id']}", {"lead_id": lead_id})
                        if resp.get("success"):
                            a["lead_id"] = lead_id
                            stats["activity_lead_id_set"] += 1
                        else:
                            stats["activity_lead_id_error"] += 1
                    except Exception as e:
                        print(f"  ERROR asignando lead_id a actividad {a['id']}: {e}")
                        stats["activity_lead_id_error"] += 1

            # asociar lead_id en las notas de Diio de esta persona
            diio_notes = get_person_diio_notes(pid)
            for n in diio_notes:
                if n.get("lead_id") == lead_id:
                    continue
                if TEST_MODE:
                    print(f"[{i}/{len(leads)}] '{title}': [TEST] asignaria lead_id a nota {n['id']}")
                    stats["note_lead_id_set"] += 1
                else:
                    try:
                        resp = api_put(f"notes/{n['id']}", {"lead_id": lead_id})
                        if resp.get("success"):
                            stats["note_lead_id_set"] += 1
                        else:
                            stats["note_lead_id_error"] += 1
                    except Exception as e:
                        print(f"  ERROR asignando lead_id a nota {n['id']}: {e}")
                        stats["note_lead_id_error"] += 1

        for channel, matches in [("aircall", aircall_matches), ("whatsapp", whatsapp_matches)]:
            for activity, new_subject in build_rename(matches):
                if TEST_MODE:
                    print(f"[{i}/{len(leads)}] '{title}' ({channel}): [TEST] '{activity.get('subject')}' -> '{new_subject}'")
                    stats["activities_renamed"] += 1
                    result_log.append({"lead_id": lead_id, "activity_id": activity["id"], "new_subject": new_subject})
                else:
                    try:
                        resp = api_put(f"activities/{activity['id']}", {"subject": new_subject})
                        if resp.get("success"):
                            stats["activities_renamed"] += 1
                            result_log.append({"lead_id": lead_id, "activity_id": activity["id"], "new_subject": new_subject})
                        else:
                            stats["activities_rename_error"] += 1
                    except Exception as e:
                        print(f"  ERROR renombrando actividad {activity['id']}: {e}")
                        stats["activities_rename_error"] += 1

    with open("lead_touch_numbering_result_log.json", "w") as f:
        json.dump(result_log, f)

    print(f"\n{'='*60}")
    print(f"Resumen: {stats['activities_renamed']} actividades renombradas "
          f"({stats['activities_rename_error']} errores)")
    print(f"         {stats['activity_lead_id_set']} lead_id asignados en actividades whatsapp "
          f"({stats['activity_lead_id_error']} errores)")
    print(f"         {stats['note_lead_id_set']} lead_id asignados en notas Diio "
          f"({stats['note_lead_id_error']} errores)")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
