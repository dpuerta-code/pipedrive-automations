#!/usr/bin/env python3
"""
Cada 5 minutos: asigna un Customer Manager (campo "CM SE") a los deals
que tienen "CM scheduled on" = hoy y "CM SE" vacio. Para no depender de
un Filter guardado en Pipedrive, la busqueda se hace con `stage_id`
(parametro nativo de la API) sobre la etapa "Scheduled Commercial
Meeting" del pipeline "Pipeline Creation" -- todo deal con "CM scheduled
on" lleno debe estar ahi, es donde el BDR lo mueve al agendar la
reunion -- y el filtro de fecha/campo vacio se aplica en Python. Los
deals con "CM scheduled on" en otro dia se recogen solos cuando llegue
su dia.

El pool de CMs es fijo, el Grupo SE (ver CM_POOL). La asignacion es por
balanceo de carga con round-robin ponderado suave (smooth weighted
round-robin, igual al que usan balanceadores como nginx): se cuenta
cuantos deals tiene cada CM en "CM SE" dentro del filtro 71373 ("CM
Load - Last 30 Days", deals con "CM scheduled on" en el ultimo mes --
se actualiza casi al instante, no espera a que la reunion se atienda)
y cada CM entra a la rotacion con un peso proporcional a cuanto le
falta para nivelarse con el que mas carga tiene. A diferencia de un
"siempre el de menor carga" puro, esto evita que una sola persona se
lleve varias asignaciones seguidas dentro de la misma corrida solo por
estar mas atras -- los turnos se reparten entre varios CMs mientras
sigue convergiendo a nivelar la carga. La carga y los acumuladores se
actualizan en memoria despues de cada asignacion.

Como en la practica casi siempre hay 1 solo deal pendiente por corrida
(el cron corre cada 5 min), el "suavizado" de arriba por si solo no
alcanza a evitar que el mismo CM se lleve varias asignaciones reales
seguidas en corridas separadas -- simplemente gana siempre el que tenga
menos carga en ese momento. Para evitar esto, `pick_next_cm` recibe
ademas el ultimo CM asignado por balanceo/continuidad (ver
`get_last_assigned_cm`, calculado en vivo desde el mismo filtro 71373)
y lo excluye de poder ganar la ronda inmediatamente siguiente, aunque
siga siendo el de menor carga -- sigue sumando peso normalmente, asi
que si continua atras gana la ronda de despues sin exclusion.

Excepcion 1 (BDR es del Grupo SE): si el BDR del deal es uno de los
mismos CMs del Grupo SE, el deal se asigna a si mismo como CM SE (no
pasa por continuidad ni por el balanceo). Estas reuniones tampoco
cuentan para la carga de 30 dias de nadie -- se excluyen por completo
del calculo de nivelacion porque no las repartio el script.

Excepcion 2 (canal organic/google): si el "Source channel" del deal es
"organic" o "google", NO entra al balanceo de carga normal. En vez de
eso usa una rotacion fija y simple, en este orden: Manoella, Fabian,
Daniel, Sofia, Andres Mayusa. Para saber a quien le toca, busca el
ultimo deal creado (por fecha de creacion) de canal organic/google que
ya tenga CM SE asignado (filtro 71381) y asigna al siguiente en la
lista. No revisa disponibilidad de calendario ni carga -- es una
rotacion pura por orden de creacion. Esta excepcion tiene prioridad
sobre la de continuidad (excepcion 3): un deal organic/google siempre
usa esta rotacion fija, incluso si su organizacion tendria continuidad.

Se puede pausar por completo con ORGANIC_GOOGLE_ROTATION_ENABLED=False --
mientras este flag sea False, los deals de canal organic/google NO se
autoasignan por ninguna via, se saltan y quedan pendientes para
asignacion manual. Reactivada 2026-08-27; ver
ORGANIC_GOOGLE_ROTATION_RESET_AT para como se reinicio limpio en
Manoella sin perder la continuidad de la rotacion hacia adelante.

Excepcion 3 (continuidad de cuenta): si la organizacion del deal tiene
un deal Lost de hace menos de 6 meses cuya ultima etapa fue "Opp" o una
etapa posterior en su pipeline (o sea, llego a ser una oportunidad real
antes de perderse), el deal se asigna al mismo CM SE que tenia ese deal
anterior, sin pasar por el balanceo. Si ese deal anterior no tiene CM SE
asignado, se sigue con el balanceo normal.

Este script SOLO asigna "CM SE" en Pipedrive -- no toca Google Calendar
ni ningun archivo del repo. La creacion del evento de calendario (con
disponibilidad de 50 min, reunion de 45 min, invitando al SE) la hace
una rutina programada en la nube aparte, que lee Pipedrive directo via
su propio MCP (no depende de este script ni de un archivo intermedio).

TEST_MODE=true -> solo calcula y muestra que asignaria, no escribe nada.
Cron: cada 5 minutos.
"""

import os
import json
import time
import requests
from collections import Counter
from datetime import date, datetime, timedelta

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

LOAD_FILTER_ID = 71373              # CM Load - Last 30 Days
ORGANIC_GOOGLE_FILTER_ID = 71381    # CM Organic-Google Rotation - Last Assigned

# Source channel (campo built-in "channel", NO es un custom field hash).
CHANNEL_GOOGLE = 283
CHANNEL_ORGANIC = 285
EXCLUDED_ROTATION_CHANNELS = {CHANNEL_GOOGLE, CHANNEL_ORGANIC}
CHANNEL_NAMES = {CHANNEL_GOOGLE: "google", CHANNEL_ORGANIC: "organic"}

# REACTIVADO 2026-08-27 21:01 UTC a pedido del usuario (estuvo pausado desde
# las 20:25 UTC del mismo dia -- ver PR #33). Mientras esto sea False, los
# deals de canal organic/google se saltan por completo y quedan pendientes
# para asignacion manual.
ORGANIC_GOOGLE_ROTATION_ENABLED = True

# El usuario pidio que la rotacion arranque limpia en Manoella al
# reactivarse, sin importar el historial real en el filtro 71381 (que a
# esta fecha mostraria a Daniel como ultimo, de una asignacion manual de
# cierre hecha por fuera del script para el deal 26473). En vez de un
# override que hay que recordar quitar despues, get_last_organic_google_cm
# ignora cualquier deal de este canal creado ANTES de este timestamp -- el
# primer deal que aparezca despues de esto no encuentra historial "valido"
# y le toca a Manoella (primera de ORGANIC_GOOGLE_ROTATION); de ahi en
# adelante la rotacion sigue normal para siempre, no hace falta tocar esto
# de nuevo.
ORGANIC_GOOGLE_ROTATION_RESET_AT = "2026-09-01T21:35:49Z"

# Orden fijo pedido por el usuario para deals de canal organic/google -- NO
# pasa por el balanceo de carga ni por disponibilidad, es un round robin
# secuencial simple basado en quien recibio el ultimo deal de este canal.
ORGANIC_GOOGLE_ROTATION = [
    21686645,  # Mano (Manoella De Andreis)
    21686656,  # Fabi (Fabian Cubillos)
    21983370,  # Daniel (Daniel Fiquitiva)
    21983447,  # Sofi (Sofia Bello)
    21983348,  # Mayu (Andres Mayusa)
]

# Etapa "Scheduled Commercial Meeting" del pipeline "Pipeline Creation" (id
# 4). Todo deal con "CM scheduled on" lleno debe estar en esta etapa -- es
# donde el BDR mueve el deal al agendar la reunion.
SCHEDULED_COMMERCIAL_MEETING_STAGE_ID = 20

CM_SE_KEY = "ae660fc6250e95791638d5e5a054b5077e2129d6"           # CM SE (user field)
BDR_KEY = "64580d9fd4762bd5d18027ee8fb90ab3a93201d3"             # BDR (user field)
CM_SCHEDULED_ON_KEY = "12a53bac7b0c1a6d7d743f6ee2ad09e567b62ed8"  # CM scheduled on (date field)

# Grupo SE: pool fijo de CMs elegibles para el balanceo, rotacion
# organic/google y continuidad.
CM_POOL = {
    21686645: "Manoella De Andreis",
    21983370: "Daniel Fiquitiva",
    21983348: "Andres Mayusa",
    21983447: "Sofia Bello",
    21686656: "Fabian Cubillos",
}

# Valentina Carrillo (agregada 2026-09-02): SOLO para la Excepcion 1
# (autoasignacion cuando ella misma es BDR y CM SE). A proposito NO forma
# parte de CM_POOL -- no debe entrar al balanceo, a la rotacion
# organic/google, ni contar como candidata de continuidad. El usuario dijo
# explicitamente que por ahora es "solo en el caso de que ella misma sea
# BDR y SE".
SELF_ASSIGN_ONLY_POOL = {
    21997527: "Valentina Carrillo",
}

# Stages con order_nr >= la etapa "Opp" de su propio pipeline (id -> nombre):
#   Expansion Deals (pipeline 1): OPP(1), Proposal Sent(2), Contract Signed(3)
#   New Deals (pipeline 2): Requisition-OPP(9), Negotiation(10), Supplier
#     Evaluation(25), Selected Supplier(17), Legal & Finance Review(26),
#     Contract Signed(27)
#   Renewal Deals (pipeline 3): OPP(12), Proposal Sent(15), Supplier
#     Evaluation(30), Selected Supplier(29), Legal & Finance Review(28),
#     Contract Signed(16)
#   Pipeline Creation (pipeline 4): no tiene etapa "Opp", no cuenta.
OPP_OR_LATER_STAGE_IDS = {1, 2, 3, 9, 10, 17, 25, 26, 27, 12, 15, 16, 28, 29, 30}

LOST_LOOKBACK_DAYS = 183  # ~6 meses

TEST_MODE = os.environ.get("TEST_MODE", "true").lower() == "true"
MAX_TEST_MODE = 5

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


def get_deals_from_filter(filter_id):
    deals, start = [], 0
    while True:
        resp = api_get("deals", {"filter_id": filter_id, "start": start, "limit": 500})
        data = resp.get("data") or []
        deals.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return deals


def get_open_deals_by_stage(stage_id):
    deals, start = [], 0
    while True:
        resp = api_get("deals", {"stage_id": stage_id, "status": "open", "start": start, "limit": 500})
        data = resp.get("data") or []
        deals.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return deals


def get_org_lost_deals(org_id):
    deals, start = [], 0
    while True:
        resp = api_get(f"organizations/{org_id}/deals", {"status": "lost", "start": start, "limit": 500})
        data = resp.get("data") or []
        deals.extend(data)
        pagination = resp.get("additional_data", {}).get("pagination", {})
        if pagination.get("more_items_in_collection"):
            start = pagination["next_start"]
        else:
            break
    return deals


def cm_user_id(deal):
    raw = deal.get(CM_SE_KEY)
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def bdr_user_id(deal):
    raw = deal.get(BDR_KEY)
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def deal_channel(deal):
    return deal.get("channel")


def get_last_organic_google_cm():
    """Busca el deal mas reciente (por fecha de creacion) de canal
    organic/google que ya tiene CM SE asignado Y que fue creado despues de
    ORGANIC_GOOGLE_ROTATION_RESET_AT. Devuelve su CM SE, o None si no hay
    ninguno todavia -- ya sea porque es la primera vez que corre esta regla,
    o porque estamos justo despues de un reinicio y todo el historial previo
    quedo descartado a proposito (ver comentario en ORGANIC_GOOGLE_ROTATION_RESET_AT)."""
    resp = api_get("deals", {"filter_id": ORGANIC_GOOGLE_FILTER_ID, "sort": "add_time DESC", "limit": 20})
    deals = resp.get("data") or []
    for d in deals:
        if d.get("add_time", "") >= ORGANIC_GOOGLE_ROTATION_RESET_AT:
            return cm_user_id(d)
    return None


def next_in_organic_google_rotation(last_cm):
    if last_cm not in ORGANIC_GOOGLE_ROTATION:
        return ORGANIC_GOOGLE_ROTATION[0]
    idx = ORGANIC_GOOGLE_ROTATION.index(last_cm)
    return ORGANIC_GOOGLE_ROTATION[(idx + 1) % len(ORGANIC_GOOGLE_ROTATION)]


def cm_scheduled_on(deal):
    raw = deal.get(CM_SCHEDULED_ON_KEY)
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def get_pending_deals_today():
    today = date.today().isoformat()
    pending = []
    for d in get_open_deals_by_stage(SCHEDULED_COMMERCIAL_MEETING_STAGE_ID):
        scheduled = cm_scheduled_on(d)
        if not scheduled or scheduled[:10] != today:
            continue
        if cm_user_id(d):
            continue
        pending.append(d)
    return pending


def deal_org_id(deal):
    raw = deal.get("org_id")
    if isinstance(raw, dict):
        return raw.get("value")
    return raw


def build_load_map():
    deals = get_deals_from_filter(LOAD_FILTER_ID)
    load = Counter({uid: 0 for uid in CM_POOL})
    for d in deals:
        if bdr_user_id(d) in CM_POOL:
            continue  # reunion autogestionada por un SE como BDR, no cuenta para nivelar
        cm_id = cm_user_id(d)
        if cm_id in CM_POOL:
            load[cm_id] += 1
    return load, deals


def get_last_assigned_cm(load_deals):
    """Entre los mismos deals que cuentan para la carga (excluye
    autoasignados BDR=SE), busca el mas reciente por 'CM scheduled on' y
    devuelve su CM SE. Se usa para no repetirle la siguiente asignacion de
    balanceo a la misma persona apenas recibio una -- sin esto, cuando solo
    hay 1 deal pendiente por corrida (el caso normal, ya que el cron corre
    cada 5 min), el "suavizado" del round-robin nunca entra en juego y el
    de menor carga se lleva varias seguidas hasta emparejarse con los
    demas, que es justo lo que el round-robin ponderado deberia evitar."""
    candidates = []
    for d in load_deals:
        if bdr_user_id(d) in CM_POOL:
            continue
        cm_id = cm_user_id(d)
        if cm_id not in CM_POOL:
            continue
        scheduled = cm_scheduled_on(d)
        if not scheduled:
            continue
        candidates.append((scheduled, d.get("update_time") or "", cm_id))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][2]


def pick_next_cm(load, current, exclude=None):
    """Smooth weighted round-robin: cada CM suma, en cada turno, un peso
    igual a (carga_maxima - su_carga + 1) -- mientras mas atras esta, mas
    peso acumula por turno. Se elige quien tenga el acumulador mas alto y
    se le resta el total de pesos, lo que lo "enfria" para los proximos
    turnos. Esto reparte las asignaciones entre varios CMs en vez de
    dárselas todas seguidas al que esta mas atras, sin dejar de converger
    a una carga nivelada.

    `exclude` (opcional): un CM que no puede ganar esta ronda aunque tenga
    el acumulador mas alto -- se usa para no repetir al ultimo asignado en
    la ronda inmediatamente anterior. Igual sigue sumando peso normal, asi
    que si vuelve a estar mas atras que los demas, gana la siguiente ronda
    ya sin exclusion."""
    max_load = max(load.values())
    weights = {cm_id: (max_load - load[cm_id]) + 1 for cm_id in load}
    for cm_id, w in weights.items():
        current[cm_id] += w
    candidates = [cm_id for cm_id in load if cm_id != exclude] or list(load.keys())
    selected = max(candidates, key=lambda cm_id: (current[cm_id], -load[cm_id]))
    current[selected] -= sum(weights.values())
    return selected


def find_continuity_cm(org_id, cutoff):
    """Busca un deal Lost de la misma organizacion, de hace menos de
    LOST_LOOKBACK_DAYS, cuya ultima etapa fue Opp o posterior en su
    pipeline. Devuelve el CM SE de ese deal (o None si no aplica)."""
    if not org_id:
        return None

    candidates = []
    for d in get_org_lost_deals(org_id):
        if d.get("stage_id") not in OPP_OR_LATER_STAGE_IDS:
            continue
        lost_time = d.get("lost_time")
        if not lost_time:
            continue
        try:
            lost_dt = datetime.strptime(lost_time[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if lost_dt < cutoff:
            continue
        candidates.append((lost_dt, d))

    if not candidates:
        return None

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    _, most_recent = candidates[0]
    return cm_user_id(most_recent)


def main():
    print(f"\n{'='*60}")
    print("CM Schedule Auto Assign")
    if TEST_MODE:
        print(f"MODO TEST: solo se procesaran los primeros {MAX_TEST_MODE} deals pendientes")
    print(f"{'='*60}\n")

    load, load_deals = build_load_map()
    print("Carga actual del Grupo SE (ultimos 30 dias):")
    for cm_id, count in load.most_common():
        print(f"  {CM_POOL[cm_id]} ({cm_id}): {count} deals")

    last_assigned_cm = get_last_assigned_cm(load_deals)
    print(f"Ultimo CM asignado por balanceo/continuidad: {CM_POOL.get(last_assigned_cm, last_assigned_cm)}")

    pending = get_pending_deals_today()
    print(f"\nDeals pendientes de asignar (CM scheduled on = hoy): {len(pending)}")

    if TEST_MODE:
        pending = pending[:MAX_TEST_MODE]

    if not pending:
        print("Nada que asignar.")
        return

    cutoff = datetime.now() - timedelta(days=LOST_LOOKBACK_DAYS)
    current = Counter({uid: 0 for uid in CM_POOL})
    stats = {
        "assigned": 0,
        "assigned_self_bdr": 0,
        "assigned_organic_google": 0,
        "assigned_continuity": 0,
        "skipped_organic_google_paused": 0,
        "error": 0,
    }
    result_log = []
    organic_google_last_cm = "unfetched"  # se busca en Pipedrive solo la primera vez que hace falta

    for i, deal in enumerate(pending, 1):
        deal_id = deal["id"]
        title = deal.get("title", "?")
        org_id = deal_org_id(deal)
        bdr_id = bdr_user_id(deal)
        channel = deal_channel(deal)

        continuity_cm = None
        is_organic_google = False
        if bdr_id in CM_POOL or bdr_id in SELF_ASSIGN_ONLY_POOL:
            chosen_cm = bdr_id
            reason = "BDR es del Grupo SE, se autoasigna"
            counts_toward_load = False
        elif channel in EXCLUDED_ROTATION_CHANNELS and not ORGANIC_GOOGLE_ROTATION_ENABLED:
            print(f"[{i}/{len(pending)}] '{title}' (deal {deal_id}): SALTADO -- rotacion organic/google pausada, queda pendiente de asignacion manual")
            stats["skipped_organic_google_paused"] += 1
            continue
        elif channel in EXCLUDED_ROTATION_CHANNELS:
            is_organic_google = True
            if organic_google_last_cm == "unfetched":
                organic_google_last_cm = get_last_organic_google_cm()
            chosen_cm = next_in_organic_google_rotation(organic_google_last_cm)
            organic_google_last_cm = chosen_cm  # avanza el puntero en memoria para el resto de esta corrida
            reason = f"canal {CHANNEL_NAMES.get(channel, channel)} -- rotacion fija Grupo SE"
            counts_toward_load = False
        else:
            continuity_cm = find_continuity_cm(org_id, cutoff)
            if continuity_cm:
                chosen_cm = continuity_cm
                reason = "continuidad (deal Lost reciente en Opp+)"
            else:
                chosen_cm = pick_next_cm(load, current, exclude=last_assigned_cm)
                reason = "balanceo (round-robin ponderado, sin repetir al ultimo asignado)"
            counts_toward_load = True
            last_assigned_cm = chosen_cm  # avanza el puntero para el resto de esta corrida

        cm_name = CM_POOL.get(chosen_cm) or SELF_ASSIGN_ONLY_POOL.get(chosen_cm) or f"user {chosen_cm}"

        if TEST_MODE:
            print(f"[{i}/{len(pending)}] '{title}' (deal {deal_id}): [TEST] asignaria a {cm_name} ({reason})")
            if counts_toward_load and chosen_cm in load:
                load[chosen_cm] += 1
            stats["assigned"] += 1
            if bdr_id in CM_POOL or bdr_id in SELF_ASSIGN_ONLY_POOL:
                stats["assigned_self_bdr"] += 1
            elif is_organic_google:
                stats["assigned_organic_google"] += 1
            elif continuity_cm:
                stats["assigned_continuity"] += 1
            result_log.append({"deal_id": deal_id, "assigned_to": chosen_cm, "reason": reason})
            continue

        try:
            resp = api_put(f"deals/{deal_id}", {CM_SE_KEY: chosen_cm})
            if resp.get("success"):
                print(f"[{i}/{len(pending)}] '{title}' (deal {deal_id}): asignado a {cm_name} ({reason})")
                if counts_toward_load and chosen_cm in load:
                    load[chosen_cm] += 1
                stats["assigned"] += 1
                if bdr_id in CM_POOL or bdr_id in SELF_ASSIGN_ONLY_POOL:
                    stats["assigned_self_bdr"] += 1
                elif is_organic_google:
                    stats["assigned_organic_google"] += 1
                elif continuity_cm:
                    stats["assigned_continuity"] += 1
                result_log.append({"deal_id": deal_id, "assigned_to": chosen_cm, "reason": reason})
            else:
                print(f"[{i}/{len(pending)}] '{title}' (deal {deal_id}): ERROR: {resp}")
                stats["error"] += 1
        except Exception as e:
            print(f"[{i}/{len(pending)}] '{title}' (deal {deal_id}): ERROR: {e}")
            stats["error"] += 1

    with open("cm_schedule_assign_result_log.json", "w") as f:
        json.dump(result_log, f)

    print(f"\n{'='*60}")
    print(
        f"Resumen: {stats['assigned']} asignados "
        f"({stats['assigned_self_bdr']} por BDR propio, {stats['assigned_organic_google']} por rotacion organic/google, "
        f"{stats['assigned_continuity']} por continuidad), "
        f"{stats['skipped_organic_google_paused']} saltados (rotacion organic/google pausada), "
        f"{stats['error']} errores"
    )
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
