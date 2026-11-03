# Pruebas y resultados

## 1. Batería determinista — **99/99 superadas (100 %)**
Local (contenedor de desarrollo) y **en GitHub Actions** (`flynnvertical/Atlas.zip`, runs
41694120442 y 41695074464, Ubuntu, Python 3.12): verde, con la prueba de *sandbox sin red* obligatoria en CI.
Evidencia completa: [`reports/pytest_resultados.txt`](../reports/pytest_resultados.txt).
Reproducir: `pip install -r requirements-dev.txt && python -m pytest tests -v`.

**Qué es real y qué no en esta batería:**
- *Real*: todo el código del agente, SQLite/FTS5, cifrado, generación y lectura de DOCX/XLSX/PDF/CSV,
  ejecución de Python en sandbox (con verificación de que **no** tiene red ni secretos), y las llamadas
  HTTP reales contra servidores locales que imitan la Bot API de Telegram, la API de GitHub y un
  proveedor LLM (prueban reintentos, respaldo 429→siguiente, conteo de tokens, webhook con secreto).
- *Simulado*: las respuestas del modelo (`ScriptedLLM`, guiones fijos) y los resultados de búsqueda
  de DuckDuckGo/Wikipedia. Estas pruebas verifican la **lógica del orquestador**, no la inteligencia
  del modelo. Eso lo mide la evaluación en vivo (sección 2).

| Categoría | Pruebas | Ejemplos de lo verificado |
|---|---|---|
| Investigación con fuentes | 6 | lectura HTML/PDF/JSON, parseo de buscador sin anuncios, respaldo a Wikipedia, avisos HTTP≥400, fuentes registradas |
| Extracción de información | 4 | PDF por página, DOCX con tablas, XLSX, CSV/JSON |
| Generación de documentos | 4 | DOCX desde markdown con tabla, XLSX con celdas numéricas, entrega por Telegram |
| Ejecución de código | 5 | pandas en sandbox, sin red, sin secretos, timeout, calculadora segura |
| Memoria entre sesiones | 8 | búsqueda sin acentos, correcciones con histórico, expiración, borrado, persistencia tras cifrar/descifrar, CLI en subprocesos, sincronización entre dos hosts |
| Validación de resultados | 5 | criterios definidos antes de ejecutar, URL inventada → autocorrección, objeción del auditor → revisión → marcado, compactación de contexto |
| Recuperación de errores | 6 | error 503 → reintento desde checkpoint, fallo tras N intentos, trabajo interrumpido recuperado por *lease*, herramientas mal invocadas, cola de salida con reintentos |
| Seguridad y autorizaciones | 21 | SSRF (6 destinos + redirecciones), traversal (3), inyección, redacción de secretos (salidas, errores LLM, mensajes), aprobación obligatoria/una sola vez/sólo el dueño, rechazo, política de código contaminado, usuarios no autorizados, webhook sin secreto |
| Persistencia de tareas | 7 | deduplicación de mensajes, programación diaria con zona horaria, tareas programadas sin duplicar, límites, cancelar/reintentar, detección de escritor concurrente |
| Costo y recursos | 6 | presupuesto por trabajo nunca excedido, tope diario antes de usar la red, cuota agotada no consume intentos, contabilidad de tokens, tareas simples sin esquemas de herramientas |

## 2. Evaluación en vivo (modelo real + internet real) — **NO EJECUTADA todavía (falta una clave)**
`scripts/live_eval.py` define 14 casos reales con criterios fijados de antemano (investigación con
URL verificada, extracción web, DOCX, XLSX con total correcto, primo 1000 con `run_python`, PDF,
inyección real alojada en este repo, aprobación + reanudación, honestidad ante datos inexistentes,
memoria entre sesiones con estado cifrado). Se suma una prueba de persistencia entre **dos runners
distintos** a través de la rama git cifrada (`persistencia-1` → `persistencia-2`).

Historial real del 2026-11-03:
| Corrida | Resultado | Causa |
|---|---|---|
| 41684273609, 41685463295 (intento 1) | No arrancó | *"The job was not started because your account is locked due to a billing issue."* (cuenta `cargazul33`) |
| 41685463295 (intento 2, ya en `flynnvertical`) | 70 ✔, 1 ✘, 1 omitida | `pandas` faltaba en `requirements.txt`; sandbox sin red no disponible en runners (unshare restringido) → **corregidos** |
| 41694120442 | Pruebas ✔ · en vivo **0/13** | Todas las llamadas al modelo: `HTTP 200 text/plain "OK"` → **GitHub Models está retirado desde 2026-07-30** |
| 41695074464 | Pruebas ✔ · en vivo 0/13 (diagnóstico confirmado) | Ídem; el error ahora se informa en vez de ocultarse |
| 41704745202 (intento 2, ya con `GROQ_API_KEY`) | Pruebas ✔ · token ✔ · en vivo **1/13** · persistencia ✘ | (1) gpt-oss invocó una herramienta en una tarea clasificada "simple" a la que no se le enviaron herramientas → Groq `400 tool_use_failed`; (2) un 429 por límite **por minuto** activó una pausa fija de 5 min que frenó el resto; (3) el modelo afirmó "lo guardé en memoria" sin llamar a `memory_save` y la verificación no lo detectó → **corregidos** (herramientas siempre disponibles, `Retry-After` respetado, cadena de 3 modelos de Groq, control de acciones afirmadas, comprobación real de la memoria en CI) |
| 41706817830 | Pruebas ✔ · token ✔ (`@AgenteSupremoflynn_bot`) · en vivo **8/13 (61,5 %)** · persistencia-1 ✔ (memoria guardada de verdad) · persistencia-2 omitida | Fallos: L02 falso negativo del evaluador (respuesta correcta "422 222,04" con espacios finos); L03 429 por minuto con espera > 25 s mezclado con un modelo no disponible → error en vez de pausa; L06 respuesta final vacía de gpt-oss; L10 pidió confirmación por texto en vez de invocar la herramienta; L14 la memoria guardada no se recuperó (búsqueda léxica "vende"≠"venta") y buscó en internet → **corregidos**; persistencia-2 se omitía cuando fallaba un job anterior → corregido |
| 41710294686 | Pruebas ✔ · token ✔ · en vivo **11/14 (78,6 %)** · persistencia-1 ✔ · persistencia-2 ✘ | L13 (aprobación real + reanudación) ✔. Fallos: L03 defecto real — el extractor de URLs cortaba `…/Rayuela_(novela)` y arrastraba `】`, y la verificación marcaba la cita como no consultada; L06 y L14 falsos negativos del evaluador (respuestas correctas con espacios Unicode U+202F); persistencia-2: **el dato sí se recuperó en la otra máquina**, pero el agente se negó a usarlo por ser "memoria no corroborada" (lo había guardado como origen=agent) → **corregidos** (extractor de URLs, `dicho_por_usuario` en `memory_save`, normalización en el evaluador y en CI) |
| 41714026458 | Pruebas ✔ · token ✔ · en vivo **3/13** · persistencia ✘ | L01–L03 ✔ (L03 confirma la corrección del extractor de URLs). Desde L04 todos los casos quedaron en cola **sin ninguna llamada al modelo**: cupo diario gratuito de Groq agotado tras 4 evaluaciones completas el mismo día. No mide la calidad del agente; se re-ejecuta al renovarse la cuota |

Un 0/13 por falta de modelo no mide la calidad del agente: desde esta versión, sin clave el CI
informa **NO EJECUTADA** (advertencia visible) en lugar de un porcentaje engañoso, y con clave el job
falla si no alcanza el 95 %.

**Para ejecutarla**: crear una clave gratuita de Groq (console.groq.com → API Keys) y cargarla como
secreto `GROQ_API_KEY` del repositorio; después, Actions → *CI + evaluación en vivo* → *Re-run*.
También en una PC con red: `GROQ_API_KEY=... python scripts/live_eval.py`.

Objetivo: ≥ 95 % de éxito en **una sola corrida**. Todavía no se alcanzó. Mejor corrida completa hasta ahora:
11/14 (78,6 %, run 41710294686); sus 3 fallos ya tienen corrección (1 defecto real, 2 falsos negativos del
evaluador), pero esas correcciones sólo están validadas en vivo para L03.

**Límite real del plan gratuito de Groq**: una evaluación completa consume una parte grande del cupo
diario de tokens; en un mismo día caben ~2–3 corridas. Para iterar más rápido, agregá un segundo proveedor
gratuito (`GEMINI_API_KEY` u `OPENROUTER_API_KEY`), que suma su propia cuota.
