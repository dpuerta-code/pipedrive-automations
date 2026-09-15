# pipedrive-weekly

Automatizaciones de Pipedrive que corren en **GitHub Actions** — sin servidor, sin dependencia de la máquina local. Todos los scripts usan la API de Pipedrive (`slang.pipedrive.com/api/v1`) con rate limiting de 78 req/10 s.

---

## Scripts activos

### Fechas de contacto

| Script | Qué hace | Schedule |
|--------|----------|----------|
| `org_contacted_sync.py` | Sincroniza **First Contact Date** en leads y organizaciones a partir de actividades reales (WhatsApp / Aircall / correo) dentro de ±60 días del Prospection Date. Limpia fechas de ciclos anteriores (gap ≥ 60 días). El lunes corre backfill completo. | Lun–Vie 5×/día · Lunes backfill 8am Colombia |
| `last_prospection_date_sync.py` | Actualiza **Last Prospection Date** en orgs según sus leads activos. No actualiza si el nuevo date está dentro de 60 días del actual y el owner es el mismo. | Lun–Vie 5×/día |

### Gestión de leads

| Script | Qué hace | Schedule |
|--------|----------|----------|
| `lead_sal_archive.py` | Cuando un deal tiene **SAL date** igual a hoy o ayer (`SAL_LOOKBACK_DAYS=2`), archiva todos los leads activos de esa org y elimina sus tareas pendientes (excepto tipo *retomar*). Genera backup JSON antes de actuar. | Lun–Vie 12pm y 7pm Colombia |
| `lead_activity_cleanup.py` | Red de seguridad: busca leads archivados en los últimos `LOOKBACK_DAYS` días (default 7) y elimina tareas pendientes sobrantes. Conserva tipo *retomar*. | Diario 7:30pm Colombia |

### Actividades Diio

| Script | Qué hace | Schedule |
|--------|----------|----------|
| `diio_whatsapp_activity.py` | Detecta notas de Pipedrive creadas por Diio con mensajes de **WhatsApp** (trigger: *"detalle de la conversación escrita de Whatsapp"*) y crea actividad tipo `whatsapp` marcada como hecha. Evita duplicados por fecha. | 3×/día — 12pm, 3pm, 7pm Colombia |
| `diio_linkedin_activity.py` | Detecta notas de Pipedrive creadas por Diio con mensajes de **LinkedIn** (trigger: *"Mensajería instantánea"* + *"cargada por diio"*) y crea actividad tipo `linkedin_conversation` marcada como hecha. | 1×/día — 7pm Colombia |

### Datos externos

| Script | Qué hace | Schedule |
|--------|----------|----------|
| `email_history_sync.py` | Reemplaza el conector nativo de Airbyte (`pipedrive.mail`, muerto desde julio 2024). Recalcula ~1700 orgs activas, llama a `/organizations/{id}/flow`, filtra `mailMessage` de los últimos 4 días e inserta en **`bpa.email_history`** (ClickHouse) en batches de 200. Deduplica por `mail_id`. | Diario 6am Colombia |
| `linkedin_size_check.py` | Para orgs con LinkedIn URL, consulta company size vía **Apify** y marca *"Menos de 50 empleados"* en ICP Non Compliance Reason si aplica. | Lun–Vie 7pm Colombia |

### Mantenimiento

| Script | Qué hace | Schedule |
|--------|----------|----------|
| `og_mirror_sync.py` | Sincroniza el campo **Organization Category** desde la org hacia la persona y el lead vinculados (filtro ID 607). | Lun–Vie 7pm Colombia |
| `pipedrive_weekly.py` | Busca deals **perdidos en los últimos 7 días** con SQL date lleno. Para cada org afectada, crea o actualiza la nota pinneada con un resumen de todos sus deals. | Viernes 9am Colombia |

---

## Scripts inactivos / one-off

| Script | Estado | Notas |
|--------|--------|-------|
| `first_contact_date_sync.py` | **Desactivado** julio 2026 | Reemplazado por `org_contacted_sync.py` |
| `org_contacted_backfill.py` | One-off | Backfill masivo desde `leads_to_backfill.json` |
| `linkedin_enrich.py` | Experimental | Enriquecimiento de orgs desde LinkedIn vía Composio |

---

## Secrets requeridos

Configurados en **Settings → Secrets → Actions** del repositorio. Ningún token está hardcodeado.

| Secret | Usado por |
|--------|-----------|
| `PIPEDRIVE_API_TOKEN` | Todos los scripts |
| `CLICKHOUSE_HOST` | `email_history_sync.py` |
| `CLICKHOUSE_PORT` | `email_history_sync.py` |
| `CLICKHOUSE_USER` | `email_history_sync.py` |
| `CLICKHOUSE_PASSWORD` | `email_history_sync.py` |

---

## TEST_MODE

Todos los scripts aceptan `TEST_MODE=true` para correr en modo solo-lectura desde **Actions → workflow_dispatch** sin modificar datos en Pipedrive.
