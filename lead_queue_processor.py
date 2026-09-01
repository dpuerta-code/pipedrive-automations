#!/usr/bin/env python3
"""
Procesa la cola de clicks del dashboard de Metabase (tab "Sheet1" de la hoja
"Cola de Leads - Prospección Claude"). Cada fila pendiente trae una `action`:

- "mark_campaign" (orgs ya matcheadas, is_match=1): busca/crea el Lead del
  programa para esa organizacion y marca el campo "Campaing" = Yes.
- "create_org_lead" (orgs "Posible New", is_match=0): busca duplicados en
  Pipedrive por nombre exacto de organizacion y por dominio de email del
  contacto; si no encuentra ninguno, crea Organizacion -> Persona -> Lead.
  Si encuentra un posible duplicado, deja la fila en 'needs_review' sin
  crear nada. Al Lead resultante tambien se le marca "Campaing" = Yes.

No depende de ClickHouse — solo Pipedrive API + Google Sheets API.

TEST_MODE=true -> solo imprime lo que haria, no escribe nada.
Cron GitHub Actions: cada hora aprox.
"""

import os
import time
import json
from datetime import datetime, timezone

import gspread
import requests

API_TOKEN = os.environ["PIPEDRIVE_API_TOKEN"]
BASE_URL = "https://slang.pipedrive.com/api/v1"

QUEUE_SPREADSHEET_ID = "1g2MVl8H17gTtmSMKPZKypPIXRwRCIboMYZycFLH9VnY"
QUEUE_TAB = "Sheet1"

LEAD_MARKER = "Prospección Claude"
CAMPAING_FIELD_KEY = "cba00ea5c8cac481d5c79d3d0d45c831d1891b47"
CAMPAING_YES = 1453

# Custom fields de Organizacion usados al crear una org nueva ("Posible New").
ORG_COUNTRY_FIELD_KEY = "6ba492e23a1d6df40dd0a1127247411b49e617f7"
ORG_HEAD_INDUSTRY_FIELD_KEY = "2d05783a484e441a0aa07224d4263573fb2e11d7"
ORG_COMPANY_SIZE_FIELD_KEY = "291c4a7d06c5315afc58342ca361db89a9d802b9"
ORG_DOMAIN_FIELD_KEY = "e316efbaae3578effff0947aa28352a86e17bf57"
ORG_WEBSITE_FIELD_KEY = "631e3c6c2b45f45ab23593b4e1707d39db88b5a3"
ORG_LINKEDIN_FIELD_KEY = "82b4cd3605c175dba7512c673946b8d4b4d83427"

ORG_COUNTRY_OPTIONS = {
    "Argentina": 315, "Bolivia": 316, "Brazil": 317, "Chile": 318, "Colombia": 319,
    "Costa Rica": 320, "Dominican Republic": 321, "Ecuador": 322, "El Salvador": 323,
    "Guatemala": 324, "Honduras": 325, "Mexico": 326, "Nicaragua": 327, "Panama": 328,
    "Paraguay": 329, "Peru": 330, "Puerto Rico": 331, "Spain": 332, "Uruguay": 333,
    "Venezuela": 334, "United States": 335, "France": 336, "Italy": 337, "Canada": 338,
    "United Kingdom": 339, "Germany": 340, "China": 341, "Sweden": 342, "Philippines": 343,
    "Netherlands": 344, "Singapore": 345, "Denmark": 346, "Belgium": 347, "Japan": 348,
    "South Africa": 349, "Korea": 350, "Norway": 351, "Malaysia": 352,
    "United Arab Emirates": 353, "Israel": 354, "Australia": 355, "Cuba": 356,
    "Other": 357, "Pakistan": 358,
}

ORG_HEAD_INDUSTRY_OPTIONS = {
    "Aviation": 622, "Agriculture": 623, "Manufacturing": 624, "Chemicals": 625,
    "Engineering & Construction Services": 626, "Consumer Products": 627,
    "Contact Center & BPO": 628, "Education": 629, "Energy & Natural Resources": 630,
    "Entertainment": 631, "Financial Services": 632, "Firm Services": 633,
    "Forest Products, Paper & Packaging": 634, "General Services": 635, "Healthcare": 636,
    "Machinery & Equipment": 638, "Professional Services": 639, "Mining": 640,
    "Public Sector": 641, "Real Estate": 642, "Retail": 643, "Social Sector": 644,
    "Technology": 645, "Telecommunications": 646, "Textiles & Leather": 647,
    "Transportation & Logistics": 648, "Travel & Tourism": 649, "Utilities": 650,
    "Construction": 1135, "Automotive & Assembly": 1134,
}

ORG_COMPANY_SIZE_OPTIONS = {
    "1-10": 373, "11-50": 374, "51-100": 375, "101-200": 376, "201-500": 377,
    "501-1000": 378, "1001-5000": 379, "5001-10000": 380,
}

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


def api_patch(endpoint, data):
    rate_limit()
    r = requests.patch(f"{BASE_URL}/{endpoint}", params={"api_token": API_TOKEN}, json=data, timeout=30)
    r.raise_for_status()
    return r.json()


def get_sheet():
    creds = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS"])
    gc = gspread.service_account_from_dict(creds)
    return gc.open_by_key(QUEUE_SPREADSHEET_ID)


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
    return items[0]["item"]["id"] if items else None


def find_existing_person(contact_nombre, email, org_id):
    return find_person_in_org_by_name(contact_nombre, org_id) or find_person_by_email(email)


def find_org_by_name(name):
    resp = api_get("organizations/search", {"term": name, "exact_match": "true"})
    items = (resp.get("data") or {}).get("items") or []
    return items[0]["item"] if items else None


def find_orgs_by_email_domain(email):
    if not email or "@" not in email:
        return []
    domain = email.split("@", 1)[1]
    resp = api_get("persons/search", {"term": domain, "fields": "email"})
    items = (resp.get("data") or {}).get("items") or []
    orgs = {}
    for it in items:
        org = (it.get("item") or {}).get("organization")
        if org and org.get("id"):
            orgs[org["id"]] = org.get("name")
    return list(orgs.items())


def create_person(nombre, email, org_id):
    body = {"name": nombre or email, "org_id": org_id}
    if email:
        body["email"] = [{"value": email, "primary": True}]
    resp = api_post("persons", body)
    return resp["data"]["id"]


def create_organization(name, country, head_industry, company_size, domain, website, org_linkedin):
    """Crea la Organizacion con todos los campos que ya tengamos disponibles:
    country, head_industry, company_size (los tres son enums, se traducen a su
    option_id), domain, website y LinkedIn de organizacion (texto libre)."""
    body = {"name": name}

    if country:
        country_id = ORG_COUNTRY_OPTIONS.get(country)
        if country_id:
            body[ORG_COUNTRY_FIELD_KEY] = country_id

    if head_industry:
        industry_id = ORG_HEAD_INDUSTRY_OPTIONS.get(head_industry)
        if industry_id:
            body[ORG_HEAD_INDUSTRY_FIELD_KEY] = industry_id

    if company_size:
        size_id = ORG_COMPANY_SIZE_OPTIONS.get(company_size)
        if size_id:
            body[ORG_COMPANY_SIZE_FIELD_KEY] = size_id

    if domain:
        body[ORG_DOMAIN_FIELD_KEY] = domain

    if website:
        body[ORG_WEBSITE_FIELD_KEY] = website
    elif domain:
        # fallback: si no tenemos website explicito pero si domain, se infiere.
        body[ORG_WEBSITE_FIELD_KEY] = f"https://{domain}"

    if org_linkedin:
        body[ORG_LINKEDIN_FIELD_KEY] = org_linkedin

    resp = api_post("organizations", body)
    return resp["data"]["id"]


def create_lead(sheet_company, org_id, person_id=None):
    body = {"title": f"{sheet_company} - {LEAD_MARKER}", "organization_id": org_id}
    if person_id:
        body["person_id"] = person_id
    resp = api_post("leads", body)
    return resp["data"]["id"]


def mark_campaing(lead_id):
    api_patch(f"leads/{lead_id}", {CAMPAING_FIELD_KEY: CAMPAING_YES})


def ensure_lead_and_campaing(org_id, sheet_company, org_category, contact_nombre, email):
    """Busca o crea el Lead del programa para una org matcheada, marca Campaing=Yes.
    Devuelve (lead_id, person_id)."""
    lead = existing_program_lead(org_id)
    if lead:
        if not TEST_MODE:
            mark_campaing(lead["id"])
        return lead["id"], lead.get("person_id")

    person_id = None
    if org_category == "Coverage":
        person_id = find_existing_person(contact_nombre, email, org_id)
        if not person_id and not TEST_MODE:
            person_id = create_person(contact_nombre, email, org_id)

    if TEST_MODE:
        print(f"    [TEST] crearia Lead para org {org_id} ('{sheet_company}') y marcaria Campaing=Yes.")
        return None, person_id

    lead_id = create_lead(sheet_company, org_id, person_id)
    mark_campaing(lead_id)
    return lead_id, person_id


def process_mark_campaign(row):
    org_id = int(row["org_id"])
    sheet_company = row["sheet_company"]
    org_category = row.get("org_category") or "Coverage"
    contact_nombre = row.get("contact_nombre", "")
    email = row.get("email", "")

    lead_id, person_id = ensure_lead_and_campaing(org_id, sheet_company, org_category, contact_nombre, email)
    return {"status": "done", "result_org_id": org_id, "result_person_id": person_id, "result_lead_id": lead_id, "error_message": ""}


def process_create_org_lead(row, mark_campaign_after=False):
    """Crea Organizacion + Persona + Lead (con chequeo de duplicados).
    mark_campaign_after=True se usa cuando esta creacion se disparo desde el
    link "Marcar Campaña" en una fila Posible New (ahi si se marca Campaing=Yes).
    Desde el link "Crear Org + Lead" normal, NO se marca Campaing."""
    sheet_company = row["sheet_company"]
    email = row.get("email", "")
    contact_nombre = row.get("contact_nombre", "")
    country = row.get("country", "")
    head_industry = row.get("head_industry", "")
    company_size = row.get("company_size", "")
    domain = row.get("domain", "")
    website = row.get("website", "")
    org_linkedin = row.get("org_linkedin", "")

    name_match = find_org_by_name(sheet_company)
    if name_match:
        return {
            "status": "needs_review", "result_org_id": "", "result_person_id": "", "result_lead_id": "",
            "error_message": f"Posible duplicado por nombre: org existente '{name_match.get('name')}' (id {name_match.get('id')})",
        }

    domain_matches = find_orgs_by_email_domain(email)
    if domain_matches:
        detalle = "; ".join(f"{n} (id {i})" for i, n in domain_matches)
        return {
            "status": "needs_review", "result_org_id": "", "result_person_id": "", "result_lead_id": "",
            "error_message": f"Posible duplicado por dominio de email: {detalle}",
        }

    if TEST_MODE:
        campaing_note = " y marcaria Campaing=Yes" if mark_campaign_after else " (sin marcar Campaing)"
        print(f"    [TEST] sin duplicados encontrados: crearia Organizacion '{sheet_company}', Persona '{contact_nombre}', Lead{campaing_note}.")
        return {"status": "done", "result_org_id": "", "result_person_id": "", "result_lead_id": "", "error_message": ""}

    org_id = create_organization(sheet_company, country, head_industry, company_size, domain, website, org_linkedin)
    person_id = create_person(contact_nombre, email, org_id) if email or contact_nombre else None
    lead_id = create_lead(sheet_company, org_id, person_id)
    if mark_campaign_after:
        mark_campaing(lead_id)

    return {"status": "done", "result_org_id": org_id, "result_person_id": person_id, "result_lead_id": lead_id, "error_message": ""}


def main():
    print(f"\n{'='*60}")
    print("Lead Queue Processor")
    if TEST_MODE:
        print("MODO TEST: solo lectura, no se escribe nada en Pipedrive ni en la hoja")
    print(f"{'='*60}\n")

    sh = get_sheet()
    ws = sh.worksheet(QUEUE_TAB)
    records = ws.get_all_records()

    pending = [(i + 2, r) for i, r in enumerate(records) if r.get("status") == "pending"]
    print(f"Filas pendientes: {len(pending)}\n")

    header = ws.row_values(1)
    col = {name: idx + 1 for idx, name in enumerate(header)}

    stats = {"done": 0, "needs_review": 0, "error": 0}

    for row_num, row in pending:
        action = row.get("action")
        sheet_company = row.get("sheet_company", "?")
        print(f"Fila {row_num} ('{sheet_company}', action={action})")

        try:
            if action == "mark_campaign":
                if not str(row.get("org_id", "")).strip():
                    # Posible New sin org_id todavia: no hay Lead que marcar,
                    # asi que "Marcar Campaña" crea Org+Persona+Lead Y marca
                    # Campaing=Yes (a diferencia de "Crear Org + Lead", que
                    # crea todo pero NO marca Campaing).
                    result = process_create_org_lead(row, mark_campaign_after=True)
                else:
                    result = process_mark_campaign(row)
            elif action == "create_org_lead":
                result = process_create_org_lead(row)
            else:
                result = {"status": "error", "result_org_id": "", "result_person_id": "", "result_lead_id": "",
                          "error_message": f"action desconocida: {action}"}
        except Exception as e:
            result = {"status": "error", "result_org_id": "", "result_person_id": "", "result_lead_id": "",
                      "error_message": str(e)}

        stats[result["status"]] = stats.get(result["status"], 0) + 1
        print(f"  -> {result['status']}" + (f" ({result['error_message']})" if result.get("error_message") else ""))

        if not TEST_MODE:
            ws.update_cell(row_num, col["status"], result["status"])
            ws.update_cell(row_num, col["processed_at"], datetime.now(timezone.utc).isoformat())
            ws.update_cell(row_num, col["result_org_id"], result["result_org_id"])
            ws.update_cell(row_num, col["result_person_id"], result["result_person_id"])
            ws.update_cell(row_num, col["result_lead_id"], result["result_lead_id"])
            ws.update_cell(row_num, col["error_message"], result["error_message"])

    print(f"\n{'='*60}")
    print(f"Resumen: {stats.get('done', 0)} completadas, {stats.get('needs_review', 0)} necesitan revision, "
          f"{stats.get('error', 0)} errores")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
