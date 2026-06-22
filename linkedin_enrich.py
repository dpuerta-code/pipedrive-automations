#!/usr/bin/env python3
"""
Test con org 52684 (ProCibernética):
- Busca en LinkedIn (URL, descripción, company size)
- Guarda LinkedIn URL si está vacío
- Guarda country si está vacío
- Actualiza nota existente agregando descripción de LinkedIn
- Si country es Colombia y empresa tiene presencia en varios países → usa count local
"""

import json, re, subprocess, requests, time

API_TOKEN = "3c7daab3fd84fcfb8a45f64c847a430a5146492d"
BASE_URL  = "https://slang.pipedrive.com/api/v1"
COMPOSIO  = "/Users/sofiapuerta/.composio/composio"

LINKEDIN_KEY = "82b4cd3605c175dba7512c673946b8d4b4d83427"
COUNTRY_KEY  = "6ba492e23a1d6df40dd0a1127247411b49e617f7"
COLOMBIA_OPT = 319

ICP_KEY         = "8396020706201279de40c71d6c2d5d8f2bc8fa6a"
ICP_OPTION_LT50 = 1428


def pd(method, path, params=None, **kwargs):
    p = {"api_token": API_TOKEN}
    if params:
        p.update(params)
    r = getattr(requests, method)(
        f"{BASE_URL}/{path}",
        params=p,
        timeout=30, **kwargs
    )
    r.raise_for_status()
    return r.json()


def composio(action, params):
    result = subprocess.run(
        [COMPOSIO, "execute", action, "-d", json.dumps(params)],
        capture_output=True, text=True, timeout=60
    )
    data = json.loads(result.stdout)
    if not data.get("successful"):
        raise RuntimeError(f"Composio error: {data}")
    return data.get("data", {})


def search_linkedin(name, country_label):
    # Paso 1: encontrar URL
    query = f"{name} {country_label} site:linkedin.com/company"
    search = composio("COMPOSIO_SEARCH_WEB", {"query": query})

    linkedin_url = None
    for c in search.get("citations", []):
        if "linkedin.com/company" in c.get("url", ""):
            linkedin_url = c["url"]
            break

    answer_text = search.get("answer", "")

    if not linkedin_url:
        return None, None, None, None, "no LinkedIn URL"

    # Paso 2: obtener datos estructurados
    fetch = composio("COMPOSIO_SEARCH_FETCH_URL_CONTENT",
                     {"urls": [linkedin_url], "max_characters": 2000})

    results = fetch.get("results", [])
    props = {}
    if results and results[0].get("entities"):
        props = results[0]["entities"][0].get("properties", {})

    description = props.get("description", "")
    hq_country  = (props.get("headquarters") or {}).get("country", "")
    workforce   = props.get("workforce") or {}
    size_global = workforce.get("total")

    # Regla Colombia: si la org es de Colombia y tiene presencia en otros países,
    # intentar extraer el count local del texto de búsqueda
    size_final = size_global
    if country_label == "Colombia" and size_global is not None:
        # Buscar si el answer menciona un número específico para Colombia
        local_match = re.search(
            r'(\d[\d,\.]*)\s*(?:employees?|empleados?).*?Colombia|Colombia.*?(\d[\d,\.]*)\s*(?:employees?|empleados?)',
            answer_text, re.IGNORECASE
        )
        if local_match:
            raw = (local_match.group(1) or local_match.group(2)).replace(",","").replace(".","")
            try:
                local_count = int(raw)
                # Solo usar local si es menor al global (indica que es un subconjunto)
                if local_count < size_global:
                    size_final = local_count
                    print(f"  → Usando count Colombia ({local_count}) en lugar de global ({size_global})")
            except ValueError:
                pass

    return linkedin_url, description, hq_country, size_final, None


def get_country_label(org):
    """Devuelve el label del país basado en el enum value."""
    country_map = {
        315: "Argentina", 316: "Bolivia", 317: "Brazil", 318: "Chile",
        319: "Colombia", 320: "Costa Rica", 321: "Dominican Republic",
        322: "Ecuador", 323: "El Salvador", 324: "Guatemala", 325: "Honduras",
        326: "Mexico", 327: "Nicaragua", 328: "Panama", 329: "Paraguay",
        330: "Peru", 331: "Puerto Rico", 332: "Spain", 333: "Uruguay",
        334: "Venezuela", 335: "United States",
    }
    val = org.get(COUNTRY_KEY)
    if val:
        try:
            return country_map.get(int(val), str(val))
        except (ValueError, TypeError):
            pass
    return None


def get_pinned_note(org_id):
    data = pd("get", f"organizations/{org_id}/notes", params={"limit": 50})
    for n in (data.get("data") or []):
        if n.get("pinned_to_organization_flag"):
            return n
    return None


def short_description(desc):
    """Recorta la descripción a ~200 caracteres en un punto natural."""
    if not desc:
        return ""
    desc = desc.strip()
    if len(desc) <= 220:
        return desc
    cut = desc[:220].rfind(". ")
    return desc[: cut + 1] if cut > 80 else desc[:220].rstrip() + "…"


# ── MAIN ──────────────────────────────────────────────────────────────────

org_id = 52684
print(f"\n{'='*60}")
print(f"Enriqueciendo org {org_id}")
print(f"{'='*60}\n")

# 1. Datos actuales de la org
org = pd("get", f"organizations/{org_id}")["data"]
name = org["name"]
current_linkedin = org.get(LINKEDIN_KEY)
current_country  = org.get(COUNTRY_KEY)
country_label    = get_country_label(org)

print(f"Nombre:   {name}")
print(f"País:     {country_label} (val={current_country})")
print(f"LinkedIn: {current_linkedin or '(vacío)'}\n")

# 2. Buscar en LinkedIn
print("Buscando en LinkedIn...")
linkedin_url, description, hq_country, size, err = search_linkedin(
    name, country_label or "")

if err:
    print(f"ERROR: {err}")
    exit(1)

print(f"URL encontrada:  {linkedin_url}")
print(f"HQ country:      {hq_country}")
print(f"Company size:    {size}")
print(f"Descripción:     {description[:120]}...\n")

# 3. Actualizar campos de la org
updates = {}

if not current_linkedin and linkedin_url:
    updates[LINKEDIN_KEY] = linkedin_url
    print("→ Guardando LinkedIn URL")

if not current_country and hq_country:
    # Intentar mapear nombre de país a ID de opción
    country_name_to_id = {v: k for k, v in {
        315: "Argentina", 316: "Bolivia", 317: "Brazil", 318: "Chile",
        319: "Colombia", 320: "Costa Rica", 321: "Dominican Republic",
        322: "Ecuador", 323: "El Salvador", 324: "Guatemala", 325: "Honduras",
        326: "Mexico", 335: "United States", 332: "Spain",
    }.items()}
    country_id = country_name_to_id.get(hq_country)
    if country_id:
        updates[COUNTRY_KEY] = country_id
        print(f"→ Guardando country: {hq_country} ({country_id})")
    else:
        print(f"→ País '{hq_country}' no mapeado, se omite")
else:
    print("→ Country ya está lleno, no se toca")

if updates:
    pd("put", f"organizations/{org_id}", json=updates)
    print("  Org actualizada ✓")
else:
    print("→ Sin cambios en campos de la org")

# 4. Actualizar nota
short_desc = short_description(description)
desc_line  = f"\n<p><em>LinkedIn: {short_desc}</em></p>"

note = get_pinned_note(org_id)
if note:
    current_content = note["content"]
    # Evitar duplicar si ya tiene la línea de LinkedIn
    if "LinkedIn:" in current_content:
        print("\n→ La nota ya tiene descripción de LinkedIn, no se modifica")
    else:
        new_content = current_content + desc_line
        pd("put", f"notes/{note['id']}", json={"content": new_content})
        print(f"\n→ Nota {note['id']} actualizada con descripción ✓")
        print(f"  Línea agregada: {desc_line.strip()[:120]}")
else:
    # Crear nota nueva
    pd("post", "notes", json={
        "content": f"<p><em>LinkedIn: {short_desc}</em></p>",
        "org_id": org_id,
        "pinned_to_organization_flag": True,
    })
    print("\n→ Nota nueva creada ✓")

# 5. ICP check
if size is not None and size < 50:
    current_icp = org.get(ICP_KEY)
    current_ids = {int(x) for x in str(current_icp).split(",") if x.strip().isdigit()} if current_icp else set()
    if ICP_OPTION_LT50 not in current_ids:
        current_ids.add(ICP_OPTION_LT50)
        pd("put", f"organizations/{org_id}", json={ICP_KEY: ",".join(str(i) for i in sorted(current_ids))})
        print(f"→ Marcada ICP <50 empleados ({size}) ✓")

print(f"\n{'='*60}")
print("Listo")
print(f"{'='*60}\n")
