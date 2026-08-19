#!/usr/bin/env python3
"""
email_history_sync.py

Sync diario de correos reales desde Pipedrive → bpa.email_history (ClickHouse).
Corre 1 vez al día (temprano). Ventana: últimos 4 días.

Alcance de orgs: recalculado cada día desde dbt_pipedrive.leads (últimos 45 días)
+ pipedrive.deals (open). ~900 orgs.

Env vars requeridas:
  PIPEDRIVE_API_TOKEN
  CLICKHOUSE_HOST       (e.g. sql-clickhouse.clickhouse.com)
  CLICKHOUSE_PORT       (default 8443)
  CLICKHOUSE_USER
  CLICKHOUSE_PASSWORD
  TEST_MODE             (default false → solo loguea, no inserta)
"""

import json
import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ── Pipedrive ──────────────────────────────────────────────────────────────
PD_TOKEN  = os.environ["PIPEDRIVE_API_TOKEN"]
PD_BASE   = "https://slang.pipedrive.com/api/v1"

# ── ClickHouse ─────────────────────────────────────────────────────────────
CH_HOST   = os.environ.get("CLICKHOUSE_HOST", "sql-clickhouse.clickhouse.com")
CH_PORT   = os.environ.get("CLICKHOUSE_PORT", "8443")
CH_USER   = os.environ.get("CLICKHOUSE_USER", "demo")
CH_PASS   = os.environ.get("CLICKHOUSE_PASSWORD", "")
CH_BASE   = f"https://{CH_HOST}:{CH_PORT}/"

TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"
LOOKBACK_DAYS = 4
INSERT_BATCH  = 200   # filas por INSERT

# ── Pipedrive rate limiting ────────────────────────────────────────────────
_req_count = 0
_req_win   = time.time()


def _rate_limit():
    global _req_count, _req_win
    _req_count += 1
    if _req_count >= 78:
        elapsed = time.time() - _req_win
        if elapsed < 10:
            time.sleep(10 - elapsed + 0.5)
        _req_count = 0
        _req_win   = time.time()


def pd_get(endpoint, params=None):
    _rate_limit()
    p = {"api_token": PD_TOKEN}
    if params:
        p.update(params)
    r = requests.get(f"{PD_BASE}/{endpoint}", params=p, timeout=30)
    r.raise_for_status()
    return r.json()


# ── ClickHouse HTTP ────────────────────────────────────────────────────────
def ch_get(sql):
    """SELECT: devuelve lista de dicts (JSONEachRow)."""
    r = requests.get(
        CH_BASE,
        params={"query": sql + " FORMAT JSONEachRow"},
        auth=(CH_USER, CH_PASS),
        timeout=60,
    )
    r.raise_for_status()
    rows = []
    for line in r.text.strip().splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def ch_insert(rows):
    """INSERT lista de dicts en bpa.email_history."""
    if not rows:
        return
    body = "\n".join(json.dumps(row, ensure_ascii=False, default=str) for row in rows)
    r = requests.post(
        CH_BASE,
        params={"query": "INSERT INTO bpa.email_history FORMAT JSONEachRow"},
        auth=(CH_USER, CH_PASS),
        headers={"Content-Type": "application/octet-stream"},
        data=body.encode("utf-8"),
        timeout=60,
    )
    r.raise_for_status()


# ── Lógica de negocio ──────────────────────────────────────────────────────
def get_org_ids():
    """Consulta ClickHouse para obtener el conjunto de org_ids activos."""
    sql = """
    SELECT DISTINCT organization_id AS org_id
    FROM (
        SELECT toInt64(organization_id) AS organization_id
        FROM dbt_pipedrive.leads
        WHERE toDate(parseDateTimeBestEffort(add_time)) >= today() - 45
          AND organization_id IS NOT NULL
        UNION ALL
        SELECT toInt64(org_id) AS organization_id
        FROM pipedrive.deals
        WHERE status = 'open' AND org_id IS NOT NULL
    )
    WHERE organization_id IS NOT NULL
    """
    rows = ch_get(sql)
    return [int(r["org_id"]) for r in rows]


def get_existing_mail_ids(mail_ids):
    """Devuelve el conjunto de mail_ids que ya están en bpa.email_history."""
    if not mail_ids:
        return set()
    ids_str = ", ".join(str(x) for x in mail_ids)
    rows = ch_get(f"SELECT mail_id FROM bpa.email_history WHERE mail_id IN ({ids_str})")
    return {int(r["mail_id"]) for r in rows}


def parse_mail_message(org_id, item_data, cutoff_dt):
    """
    Recibe el dict 'data' de un item con object='mailMessage'.
    Devuelve un dict listo para insertar, o None si es más antiguo que cutoff.
    """
    msg_time_str = item_data.get("message_time") or ""
    if not msg_time_str:
        return None

    try:
        msg_dt = datetime.strptime(msg_time_str[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    if msg_dt < cutoff_dt:
        return None  # señal para detener paginación

    frm = (item_data.get("from") or [{}])[0]
    to_list = item_data.get("to") or []

    to_emails  = [t.get("email_address") or "" for t in to_list]
    to_names   = [t.get("name") or "" for t in to_list]
    to_person_ids = [int(t["linked_person_id"]) for t in to_list
                     if t.get("linked_person_id") is not None]

    return {
        "organization_id": int(org_id),
        "mail_id":         int(item_data["id"]),
        "subject":         item_data.get("subject") or "",
        "from_email":      frm.get("email_address") or "",
        "from_name":       frm.get("name") or "",
        "from_linked_person_id": (int(frm["linked_person_id"])
                                  if frm.get("linked_person_id") is not None else None),
        "to_emails":       to_emails,
        "to_names":        to_names,
        "to_linked_person_ids": to_person_ids,
        "message_time":    msg_time_str[:19],
        "user_id":  (int(item_data["user_id"]) if item_data.get("user_id") is not None else None),
        "deal_id":  (int(item_data["deal_id"]) if item_data.get("deal_id") is not None else None),
        "lead_id":  item_data.get("lead_id"),
    }


def fetch_org_mails(org_id, cutoff_dt):
    """
    Llama a /organizations/{id}/flow y devuelve (rows, reached_cutoff).
    reached_cutoff=True cuando vio ítems anteriores al corte → paramos.
    """
    rows = []
    start = 0
    MAX_PAGES = 10

    for _ in range(MAX_PAGES):
        try:
            resp = pd_get(f"organizations/{org_id}/flow", {"limit": 500, "start": start})
        except Exception as e:
            return rows, True, str(e)

        items = resp.get("data") or []
        stop = False

        for item in items:
            if item.get("object") != "mailMessage":
                continue
            parsed = parse_mail_message(org_id, item.get("data") or {}, cutoff_dt)
            if parsed is None:
                # Más viejo que el corte — no hay nada más útil en esta paginación
                stop = True
                continue
            rows.append(parsed)

        pag = resp.get("additional_data", {}).get("pagination", {})
        if stop or not pag.get("more_items_in_collection"):
            break
        start = pag.get("next_start", start + 500)

    return rows, False, None


def insert_in_batches(rows):
    total = 0
    for i in range(0, len(rows), INSERT_BATCH):
        batch = rows[i:i + INSERT_BATCH]
        ch_insert(batch)
        total += len(batch)
    return total


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    now_utc  = datetime.now(timezone.utc)
    cutoff_dt = (now_utc - timedelta(days=LOOKBACK_DAYS)).replace(tzinfo=None)

    print(f"\n{'='*60}")
    print(f"Email History Sync — {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if TEST_MODE:
        print("MODO TEST: no se insertan filas")
    print(f"Ventana: últimos {LOOKBACK_DAYS} días (desde {cutoff_dt.strftime('%Y-%m-%d')})")
    print(f"{'='*60}\n")

    # 1. Obtener orgs
    print("Paso 1: Obteniendo org_ids desde ClickHouse...", flush=True)
    org_ids = get_org_ids()
    print(f"  {len(org_ids)} orgs a revisar\n", flush=True)

    # 2. Recorrer orgs, recolectar correos candidatos
    print("Paso 2: Consultando Pipedrive /flow por org...", flush=True)
    all_candidates = []
    failed_orgs = []

    for i, org_id in enumerate(org_ids, 1):
        if i % 100 == 0 or i == len(org_ids):
            print(f"  [{i}/{len(org_ids)}] procesando...", flush=True)

        rows, _, error = fetch_org_mails(org_id, cutoff_dt)
        if error:
            failed_orgs.append((org_id, error))
        all_candidates.extend(rows)

    print(f"\n  Correos candidatos (últimos {LOOKBACK_DAYS} días): {len(all_candidates)}", flush=True)
    print(f"  Orgs con error: {len(failed_orgs)}\n", flush=True)

    if not all_candidates:
        print("Nada que insertar.")
        _print_summary(len(org_ids), 0, 0, failed_orgs)
        return

    # 3. Deduplicar contra ClickHouse
    print("Paso 3: Verificando duplicados en ClickHouse...", flush=True)
    candidate_ids = [r["mail_id"] for r in all_candidates]
    existing_ids  = get_existing_mail_ids(candidate_ids)

    new_rows = [r for r in all_candidates if r["mail_id"] not in existing_ids]
    print(f"  Ya existentes: {len(existing_ids)}  |  Nuevos: {len(new_rows)}\n", flush=True)

    # 4. Insertar
    print("Paso 4: Insertando en bpa.email_history...", flush=True)
    inserted = 0
    if new_rows:
        if TEST_MODE:
            print(f"  [TEST] Habría insertado {len(new_rows)} filas")
            inserted = len(new_rows)
        else:
            inserted = insert_in_batches(new_rows)
            print(f"  {inserted} filas insertadas", flush=True)

    _print_summary(len(org_ids), len(all_candidates), inserted, failed_orgs)


def _print_summary(orgs, candidates, inserted, failed):
    print(f"\n{'='*60}")
    print(f"Resumen:")
    print(f"  Orgs revisadas:    {orgs}")
    print(f"  Correos en rango:  {candidates}")
    print(f"  Correos nuevos:    {inserted}")
    print(f"  Orgs con error:    {len(failed)}")
    if failed:
        for org_id, err in failed[:10]:
            print(f"    org {org_id}: {err}")
        if len(failed) > 10:
            print(f"    ... y {len(failed) - 10} más")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
