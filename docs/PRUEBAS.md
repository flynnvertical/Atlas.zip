# Pruebas y resultados

## 1. Batería determinista — **72/72 superadas (100 %)**, ejecutada el 2026-11-03 17:32 UTC
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

## 2. Evaluación en vivo (modelo real + internet real) — **NO EJECUTADA**
`scripts/live_eval.py` define 14 casos reales con criterios fijados de antemano (investigación con
URL verificada, extracción web, DOCX, XLSX con total correcto, primo 1000 con `run_python`, PDF,
inyección real alojada en este repo, aprobación + reanudación, honestidad ante datos inexistentes,
memoria entre sesiones con estado cifrado).

Se intentó ejecutarla en GitHub Actions (único entorno con salida a internet y modelo gratuito):
**GitHub no inició los trabajos**: *"The job was not started because your account is locked due
to a billing issue."* (run 41684273609). Este contenedor de desarrollo tiene la red restringida y no
alcanza a Telegram ni a ningún proveedor de modelos, así que **no hay resultados en vivo** que reportar.

**Para ejecutarla**: resolver la facturación de GitHub y hacer push (el workflow `ci.yml` corre las
pruebas, la evaluación en vivo y una prueba de persistencia entre dos máquinas distintas), o en una
PC con red: `GITHUB_MODELS_TOKEN=... python scripts/live_eval.py`.

Objetivo: ≥ 95 % de éxito. No se asume: el resultado dependerá del modelo gratuito disponible.
