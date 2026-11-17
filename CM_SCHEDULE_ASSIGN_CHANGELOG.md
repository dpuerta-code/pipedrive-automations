# Changelog — CM Schedule Auto Assign (Cloud)

Historial completo y explícito de todos los cambios de la rutina cloud **"CM Schedule Auto Assign (Cloud)"** (`trig_01V67Cf4p7YP4SwNdogTLGki`), la automatización que asigna el campo **"CM SE"** en Pipedrive a deals pendientes revisando disponibilidad de calendario antes de asignar. Esta rutina reemplazó al scheduler de GitHub Actions de `cm_schedule_assign.py` (corridas atrasadas por horas, confirmado 2026-08-28) y agrega el chequeo de calendario que el script del repo no tiene.

Este archivo es la **fuente de verdad del historial**: el prompt de la rutina mantiene solo las reglas vigentes (con los cambios históricos condensados) y apunta aquí para el detalle completo. Cada entrada de abajo es el texto explícito original, tal como vivió en el prompt. Las copias exactas de cada versión completa del prompt (v19–v27) se conservan además localmente versionadas.

**Leyenda:** 🟢 = regla vigente hoy · ⚪ = histórico, ya no aplica.

---

## 2026-09 — Fundación

### 🟢 CAMBIO 2026-09-22 — chequeo de disponibilidad ANTES de asignar
Se agregó chequeo de disponibilidad ANTES de asignar. Motivo: un deal (Tekus, 26578) se le asignó a Fabián Cubillos por balanceo sin revisar su calendario, y Fabián ya tenía otra reunión (NAMASTEC) que se solapaba 15 min con la de Tekus. El usuario pidió explícitamente que la asignación SÍ revise disponibilidad antes de elegir, no después.

Regla de disponibilidad (misma que usa "CM Calendar Event Creator"): un espacio del CM SE cuenta como "ocupado" SOLO si existe un evento en su calendario en la ventana Y su propio responseStatus en ese evento es "accepted" (o el evento no tiene lista de attendees, ej. un evento personal). Si su responseStatus es needsAction/declined/tentative, el espacio cuenta como LIBRE aunque el evento se vea superpuesto. Ventana a revisar: 50 minutos desde la hora de la reunión (la reunión real dura 45 min).

### ⚪ CAMBIO 2026-09-25 (actualizado 2026-10-13) — cuota temporal de Valentina (quitada)
Valentina Carrillo es miembro pleno del Grupo SE — participa en rotación organic/google y balanceo normal igual que cualquier otro CM. Tuvo una cuota temporal (2026-09-23 a 2026-10-23) que ya se quitó — ya no tiene ningún trato especial.

---

## 2026-10 — Octubre

### ⚪ CAMBIO 2026-10-01 (quitado 2026-10-14) — excepción Ironman/Champion (quitada)
Hubo una Excepción que asignaba cuentas Ironman/Champion DIRECTO a un coach de SE fijo por BDR, saltando carga/continuidad/balanceo. El usuario pidió quitarla — esas cuentas ahora entran al pool normal (continuidad, después balanceo) como cualquier otro deal, sin trato especial.

### 🟢 CAMBIO 2026-10-13 — chequeo en 2 partes (puntual + Out of Office)
El chequeo de disponibilidad ahora tiene 2 partes. (a) Chequeo de horario puntual (como antes): SOLO aplica a balanceo y rotación organic/google. (b) Chequeo de Out of Office (día completo, NUEVO): aplica también a continuidad de cuenta — si el CM que tocaría por continuidad está OOO ese día, NO se le fuerza la asignación, se cae a un fallback de balanceo normal (`fallback_order`). Autoasignación BDR=SE y la regla temporal (histórica) siguen siendo de identidad fija pura, sin ningún chequeo.

### 🟢 CAMBIO 2026-10-13 (b) — segunda forma de continuidad: SQL date
Segunda forma de continuidad de cuenta (ver PR #4 de este repo). Si no hay continuidad por deal Lost, se revisa si la organización tiene OTRO deal (abierto, ganado o perdido, excluyendo el deal actual) con "SQL date" dentro de los últimos 60 días que ya tenga CM SE asignado — si lo hay, se asigna a ese mismo CM. Ver `SQL_DATE_KEY` y `find_sql_date_continuity`.

### 🟢 CAMBIO 2026-10-14 — zona horaria real desde "Country"
Se corrigió cómo se calcula `meeting_timezone`. Antes se usaba el "timezone_name" que trae el propio campo "Comm. Meeting Schedule Time" de Pipedrive, pero ese campo SIEMPRE trae "America/Bogota" (es el timezone default de la cuenta, no refleja la zona horaria real del cliente) — eso causaba que el chequeo de disponibilidad revisara la hora equivocada para deals de clientes fuera de Colombia (ej. un deal de México a las 14:00 es la hora de Ciudad de México, NO de Bogotá — son 15:00 Bogotá, 1 hora de diferencia). Ahora se calcula la zona horaria real a partir del campo "Country" del deal, usando la misma tabla país→zona horaria que usa "CM Calendar Event Creator" (`COUNTRY_TIMEZONE_MAP`). Si el país no está en la tabla o viene vacío, cae a America/Bogota como respaldo y se anota como aviso en el reporte.

### 🟢 CAMBIO 2026-10-15 — cola de reposición organic/google
La rotación fija organic/google ahora respeta una cola de reposición (`ORGANIC_GOOGLE_PENDING_REPOSITION`, mantenida a mano). Si al CM que le toca por turno normal no tiene espacio, se salta (como ya pasaba) PERO esa persona queda en la cola de reposición con prioridad para el PRÓXIMO deal de este canal, en vez de esperar a que el ciclo normal le vuelva a dar su turno. El turno normal (`og_ptr`/`ORGANIC_GOOGLE_ROTATION_RESET_TO`) NO avanza cuando alguien recibe un deal POR REPOSICIÓN — solo avanza cuando alguien recibe por su turno normal real, para no romper el ciclo. Como la rutina no guarda estado entre corridas, la cola se mantiene a mano — el reporte de cada corrida dice explícitamente a quién agregar o quitar.

Ese mismo día se corrigió el filtro 71381 ("CM Organic-Google Rotation - Last Assigned") para que ya no esté limitado al stage "Scheduled Commercial Meeting", y hubo un REBALANCEO MANUAL: los últimos organic/google reales fueron Sofía ×4, Daniel ×2, Mayusa ×1, y CERO para Manoella, Fabián y Valentina — se precargó la cola de reposición con [Manoella, Fabián, Mayusa, Valentina].

### ⚪ CAMBIO 2026-10-19 — bolsa temporal Marketing Event + Evento Buk 2026 (eliminada 2026-11-13)
Nueva bolsa temporal para deals de canal "Marketing Event" (`channel == 275`) que además tengan el label "Evento Buk 2026" (`label_ids` contiene `1459`) — vienen de un evento puntual (People Day by Buk, 15 oct 2026) y el usuario pidió repartirlos parejo entre el Grupo SE, contando TAMBIÉN los que se asignen a mano por fuera de la rutina. A diferencia de organic/google (rotación de TURNO FIJO), esta bolsa era BALANCEO PONDERADO: cada vez que tocaba asignar uno, iba para quien tuviera MENOS deals de esta bolsa específica hasta el momento. SÍ revisaba disponibilidad de calendario antes de asignar, pero NO tenía cola de reposición propia. Tenía prioridad sobre continuidad de cuenta. Regla temporal: solo aplicaba a deals con "CM scheduled on" ≤ 2026-11-30.

### 🟢 CAMBIO 2026-10-19 (b) — tercera forma de continuidad: deal Won por owner
Tercera forma de continuidad de cuenta, y la de MAYOR prioridad de las tres (se revisa ANTES que continuidad por Lost y que continuidad por SQL date). Si la organización tiene algún deal en status Won cuyo "owner" (el campo nativo de Pipedrive "Deal owner", NO el custom field "CM SE") sea alguno de los 6 del Grupo SE, el candidato se asigna a ese mismo CM — sin límite de tiempo (es una relación de cuenta duradera). Si hay varios deals Won con distintos owners del Grupo SE, se usa el más reciente por `won_time`. Respeta el chequeo de Out of Office (si el CM "ideal" está OOO ese día, cae al fallback de balanceo).

### 🟢 CAMBIO 2026-10-20 — Reglas de Oro #1 y #2
**Regla de Oro #1:** si un deal ya tiene "CM SE" asignado, NUNCA se cambia. Justo ANTES de escribir cualquier CM SE, se vuelve a consultar el deal vía `getDeal` para confirmar que el campo siga vacío — alguien pudo haberlo asignado a mano mientras tanto.
**Regla de Oro #2:** decide primero, escribe UNA sola vez. Máximo una escritura de CM SE por deal por repetición. Si hay duda de un dato, se resuelve con un `getDeal` real ANTES de escribir. Motivo: el deal 26926 (Reciclados Industriales) se escribió 3 veces en la misma repetición porque se agregó a mano con un `label_ids` inventado.
**Prohibido fabricar campos:** nunca tipear a mano valores de campos de un deal — todo tiene que venir de una llamada real a Pipedrive hecha en ese momento.

### ⚪ CAMBIO 2026-10-20 (b) — pausa temporal de Valentina en balanceo (expiró 2026-10-23)
Pausa temporal de Valentina Carrillo SOLO en el balanceo normal (`rank_balanceo`) — pedido por el usuario, hasta el viernes 2026-10-23 inclusive. NO aplicaba a organic/google, ni a la bolsa Buk, ni a continuidad. Era una despriorización de "último recurso", no un bloqueo duro. Se desactivó sola (`BALANCEO_PAUSED_CMS`/`BALANCEO_PAUSE_UNTIL`, el mecanismo genérico sigue en el script para futuros usos).

### 🟢 CAMBIO 2026-10-21 — candidatos atrasados también cuentan
Se corrigió la regla de candidato — antes exigía que "CM scheduled on" fuera EXACTAMENTE la fecha de hoy, lo que significaba que un deal que no se alcanzara a asignar el mismo día quedaba invisible PARA SIEMPRE al pasar la medianoche. Motivo real: los deals 26954 (MGM Ingeniería), 26955 (PD-BUK26 ISEC Colombia) y 26956 (Grupo Euromotors) quedaron con "CM scheduled on" = 2026-10-20 sin CM SE, y al llegar el 2026-10-21 la corrida los reportó como "0 candidatos". Ahora un deal es candidato si "CM scheduled on" es HOY O CUALQUIER FECHA ANTERIOR con CM SE vacío (cambio de `sched[:10] != today` a `sched[:10] > today`).

### 🟢 CAMBIO 2026-10-21 (b) — secuenciación dentro de la misma corrida
Se corrigió un bug real de secuenciación — cuando varios deals de balanceo (u organic/google) caen en la MISMA repetición, el script calculaba el orden de candidatos de TODOS usando la misma foto estática de carga/turno. Resultado real: 3 deals atrasados (26954, 26955, 26956) cayeron juntos y los 3 terminaron en la MISMA persona (Daniel Fiquitiva). Ahora el script SÍ actualiza `load`/`current`/`og_ptr` DENTRO del mismo for-loop, asumiendo que el primero del orden calculado será el elegido antes de pasar al siguiente deal — es una aproximación (el paso de calendario puede terminar eligiendo a otro), pero ya reparte de verdad.

### ⚪ CAMBIO 2026-10-21 (c) — identificación de la bolsa Buk por channel + SAL date (bolsa eliminada 2026-11-13)
Se corrigió cómo se identificaba un deal de la bolsa Buk. Primero se encontró que `BUK_EVENT_LABEL_ID` apuntaba a 1459 ("Evento Buk 2026", un label sin uso real) en vez de 1465 ("People Day by Buk 2026") — con el ID viejo la bolsa nunca se activó y los 23 deals PD-BUK26 existentes se repartieron sin ese balanceo (Valentina y Manoella con 6 cada una vía asignación manual de BDR, el resto con 2 cada uno). Después, el usuario pidió un criterio más confiable que un label: un deal calificaba si su "channel" nativo era Marketing Event (275) Y su campo "SAL date" (distinto de "SQL date") era ≥ 2026-10-01. Se verificó contra los 23 deals PD-BUK26 reales: los 23 tenían "SAL date" lleno, mientras que "SQL date" estaba vacío en la gran mayoría y el label no siempre se puso al crear el deal.

### 🟢 CAMBIO 2026-10-23 — filtro 71373 ahora mide carga REAL atendida
Se corrigió el filtro guardado de Pipedrive **71373** ("CM Load - Last 30 Days") — NO fue un cambio de código, fue un cambio en la definición del filtro dentro de Pipedrive. Antes filtraba por "CM scheduled on" (la fecha en que un CM fue asignado, NO el día de la reunión), así que `load` contaba CUALQUIER deal asignado en los últimos 30 días, incluyendo reuniones futuras que no se habían dado. El usuario lo detectó comparando contra un reporte que usa "Comm. Meeting attended date" y encontró un panorama totalmente distinto (ej. Andrés Mayusa resultaba con la carga MÁS alta real, no la más baja). El filtro ahora filtra por "Comm. Meeting attended date". `CM_SCHEDULED_ON_KEY` sigue usándose para identificar candidatos y como desempate en `last_assigned_cm` — ahí sí importa cuándo se asignó.

### ⚪ CAMBIO 2026-10-26 y (b) — secuencia fija para nivelar a Manoella (reemplazada por (c))
Excepción temporal para corregir un desbalance real al proyectar el cierre de octubre — Manoella quedaba muy por debajo (~8 vs 11-14). Se probó una secuencia fija cíclica (Daniel → Manoella → Fabián → Manoella → Valentina → Manoella), pero el pointer que la mantenía entre corridas tenía un bug real: contaba desde `load_deals`, que tras CAMBIO 2026-10-23 filtra por "attended date" — los deals recién asignados tienen reunión a futuro, así que el conteo se quedaba en 0 para siempre y 4 corridas seguidas terminaron todas en Daniel. El usuario pidió dejar esos deals sin tocar y reemplazó la secuencia por la regla (c).

### 🟢 CAMBIO 2026-10-26 (c) — reunión en octubre 2026 → directo a Valentina (autoexpirable)
Cualquier deal candidato cuya reunión real ("Comm. Meeting Schedule") caiga en octubre de 2026 se asigna DIRECTO a Valentina Carrillo — sin importar canal, con la única excepción de BDR=SE. SÍ revisa disponibilidad (OOO + puntual); si Valentina queda ocupada/OOO, cae al fallback de balanceo normal. La regla NO depende de la fecha de hoy — depende de la fecha de la REUNIÓN, así que se desactiva sola en cuanto deje de haber deals con reunión en octubre. (Sigue en el código; en la práctica ya casi no puede activarse.)

### ⚪ CAMBIO 2026-10-29 — conteo de la bolsa Buk por campaña total (bolsa eliminada 2026-11-13)
El usuario pidió que el reparto de la bolsa Buk se basara en reuniones REALMENTE ATENDIDAS de TODA la campaña, no solo de los últimos 30 días (los deals tempranos de la campaña se "caían" de la ventana rodante del filtro 71373, rompiendo la justicia del reparto). Se creó el filtro permanente **72765** ("CM Buk Bag - Campaign Total"): todos los deals (cualquier status, sin ventana de tiempo) con channel Marketing Event (275) y "SAL date" ≥ 2026-10-01. `buk_bag_count` contaba un deal solo si además tenía "Comm. Meeting attended date" con valor. El filtro 72765 sigue existiendo en Pipedrive pero ya no se consulta.

---

## 2026-11 — Noviembre

### 🟢 CAMBIO 2026-11-06 — ENDURECIMIENTO: nunca reconstruir datos "de memoria"
Si a mitad de una repetición se detecta que un dataset quedó en el archivo equivocado, la ÚNICA forma permitida de corregirlo es volver a llamar a Pipedrive y sobreescribir con ese resultado fresco — NUNCA reescribir el archivo "de memoria". Motivo real: en la corrida del deal 27139 (MX: greenpaper 2026), la rutina reconstruyó `/tmp/stage20_deals.json` a mano con "Comm. Meeting Schedule Time" transcrito mal ("11:00:00" en vez del real "10:00:00") — el chequeo de calendario revisó la ventana equivocada para TODOS los candidatos y asignó a Andrés Mayusa, que SÍ tenía un conflicto real a las 10:00; Valentina, libre a la hora real, debió haber quedado. Se corrigió a mano.

### 🟢 CAMBIO 2026-11-06 (b) — mandato de jq para resultados grandes
Cuando una llamada a Pipedrive devuelve un resultado demasiado grande y queda guardado en archivo aparte, la ÚNICA forma permitida de pasarlo a los `/tmp/*.json` es `jq -c '.data' <archivo> > /tmp/target.json` vía Bash — NUNCA el tool Write para "copiarlo" a mano. jq copia el dato EXACTO, byte por byte, sin margen de transcripción. Write sigue siendo válido solo para ensamblar resultados PEQUEÑOS vistos completos en el mismo turno.

### 🟢 CAMBIO 2026-11-06 (c) — la Regla de Oro #1 re-verifica fecha/hora de la reunión
El `getDeal` obligatorio antes de cada escritura ahora TAMBIÉN pide "Comm. Meeting Schedule" y "Comm. Meeting Schedule Time" y los compara contra lo que se usó para el chequeo de calendario — si no coinciden exactamente, se detiene, recalcula timezone y repite el chequeo de disponibilidad COMPLETO con la ventana correcta para el mismo orden de candidatos. Motivo real: los deals 27139 (hora mal transcrita) y 26889 (PA: Banesco, fecha mal transcrita: "2026-11-06" vs real "2026-11-09") se asignaron a la persona equivocada en la misma corrida; el getDeal de la Regla #1 se ejecutó pero nunca comparaba fecha/hora.

### 🟢 CAMBIO 2026-11-09 — `last_assigned_cm` desde stage20 (fix "no dos veces seguidas")
Antes `last_assigned_cm` se calculaba desde `load_deals` (filtro 71373), que filtra por "attended date" y NO incluye deals recién asignados con reunión futura — el mecanismo `exclude=last_assigned_cm` de `rank_balanceo` nunca se activaba entre repeticiones/corridas. Motivo real: el 2026-11-09 Fabián Cubillos ganó 5 decisiones de balanceo SEGUIDAS (deals 27153, 26966, 27167, 27168) porque cada repetición "olvidaba" que él fue el último asignado. Ahora se calcula desde `stage20` (traído fresco cada repetición) — mismo diagnóstico y fix que CAMBIO 2026-10-26 (b).

### 🟢 CAMBIO 2026-11-10 — carga combinada: atendidas + pendientes
`load` (la carga ponderada de `rank_balanceo`) ahora también cuenta compromisos PENDIENTES, no solo reuniones ya atendidas. Antes alguien recién asignado con varias reuniones a futuro seguía viéndose "el menos cargado" durante días/semanas — la causa de fondo de la racha de Fabián del 11-09. El usuario lo detectó comparando con un reporte que separa "CM_att" de "CM_pendientes_futuro". Ahora, además del conteo de `load_deals`, se suma +1 por cada deal de `stage20` que ya tiene CM SE, reunión HOY o a futuro, y sin "attended date" (para no duplicar una reunión atendida hoy mismo). Misma exclusión BDR=SE de siempre.

### 🟢 CAMBIO 2026-11-13 — eliminación de la bolsa Buk
Se ELIMINÓ por completo la bolsa temporal Marketing Event + Evento Buk 2026. Los deals de esa campaña ya NO tienen reparto aparte — entran al flujo normal (BDR=SE → organic/google si aplica → continuidad → balanceo normal con carga combinada). Se quitó: el paso 2d (fetch del filtro 72765), el archivo `buk_bag_all_deals.json` (quedan 5 archivos de entrada), las constantes `BUK_BAG_*`/`MARKETING_EVENT_CHANNEL`, las funciones `is_buk_bag_deal`/`rank_buk_bag`/`sal_date`, el conteo `buk_bag_count`, la rama en el cascade, y su sección de reporte. "SAL date" se quitó de los fetches.

### 🟢 CAMBIO 2026-11-13 (b) — early-exit (ahorro de consumo)
Cada repetición trae PRIMERO `stage20_deals` y corre el chequeo de candidatos; si hay 0 (el caso más común), la repetición termina ahí sin traer `load_deals`/`organic_google_deals`, sin escribir ni correr el script, y sin los pasos de continuidad/calendario. No cambia ninguna decisión de asignación.

### 🟢 CAMBIO 2026-11-13 (c) — proyección mínima al guardar resultados inline (ahorro de consumo)
Cuando un resultado de Pipedrive llega inline y hay que guardarlo vía Write, se guarda SOLO el subconjunto de campos que el script usa (lista exacta por archivo en el prompt), copiado tal cual del tool_result — nunca el deal completo. Motivo real: el 2026-11-12 dos Writes de ~50KB (deals completos) contribuyeron a agotar el límite semanal de la cuenta. No relaja ninguna regla anti-fabricación ni el mandato de jq.

### 🟢 CAMBIO 2026-11-16 — changelog movido a este archivo; prompt condensado
Los párrafos de changelog del prompt que documentaban cambios ya inactivos (cuota de Valentina, excepción Ironman/Champion, toda la historia de la bolsa Buk, la secuencia fija de octubre, la pausa de Valentina) se condensaron a 1-3 líneas cada uno dentro del prompt, con referencia a este archivo como fuente del historial completo. Ninguna regla vigente se tocó — solo historia. Objetivo: reducir el consumo de tokens de cada corrida (~10 corridas/día procesaban el texto completo).

---

## Decisión de asignación vigente (cascade, en orden)

1. Regla temporal histórica (reunión 18–21 sept 2026 → Valentina; ya no puede activarse).
2. **BDR=SE** → autoasignación al mismo BDR, sin chequeo de calendario.
3. **Reunión en octubre 2026** → Valentina (autoexpirable, ya casi inactiva).
4. **Canal organic (285) / google (283)** → rotación fija con cola de reposición.
5. **Continuidad** (en orden de prioridad): deal Won con owner del Grupo SE → deal Lost reciente en Opp+ (183 días) → otra oportunidad con SQL date en los últimos 60 días.
6. **Balanceo normal**: round-robin ponderado por carga combinada (atendidas últimos 30 días + pendientes futuras), sin repetir al último asignado.

Todo (salvo BDR=SE y la regla temporal) pasa por el chequeo de disponibilidad: OOO de día completo + conflicto puntual en la ventana [hora reunión, +50 min] en la zona horaria real del cliente (por "Country"), donde "ocupado" = evento con responseStatus "accepted" o sin lista de attendees.

## Pool actual (Grupo SE)

| ID Pipedrive | Nombre | Email |
|---|---|---|
| 21686645 | Manoella De Andreis | m.deandreis@slangapp.com |
| 21983370 | Daniel Fiquitiva | d.fiquitiva@slangapp.com |
| 21983348 | Andrés Mayusa | a.mayusa@slangapp.com |
| 21983447 | Sofía Bello | s.bello@slangapp.com |
| 21686656 | Fabián Cubillos | f.cubillos@slangapp.com |
| 21997527 | Valentina Carrillo | v.carrillo@slangapp.com |

## Filtros guardados de Pipedrive en uso

| Filtro | Nombre | Uso |
|---|---|---|
| 71373 | CM Load - Last 30 Days | carga atendida (por "Comm. Meeting attended date", desde CAMBIO 2026-10-23) |
| 71381 | CM Organic-Google Rotation - Last Assigned | pointer de la rotación organic/google |
| 72765 | CM Buk Bag - Campaign Total | ⚪ sin uso desde 2026-11-13 (se conserva en Pipedrive) |
