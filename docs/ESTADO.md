# Registro de estado — 2026-11-04 (actualizado tras el paquete de perfeccionamiento)

## Funciona y está comprobado (con evidencia en esta sesión)
| Capacidad | Evidencia |
|---|---|
| Orquestador: plan con criterios de éxito → herramientas → verificación → autocorrección → entrega | `tests/test_agent.py` (17 pruebas) |
| Investigación web con fuentes registradas, anti-SSRF, anti-inyección | `tests/test_tools.py` |
| Documentos PDF/DOCX/XLSX/CSV/JSON (lectura y generación) | `tests/test_tools.py` |
| Python aislado sin red ni secretos (namespace de red verificado) | `test_run_python_has_no_network`, `test_run_python_has_no_secrets` |
| Memoria persistente con búsqueda, correcciones, expiración y borrado | `tests/test_memory_state.py` |
| Estado cifrado y portable entre máquinas (CLI, sincronización vía API de GitHub) | `test_full_state_pack_unpack_via_cli`, `test_state_sync_roundtrip_between_two_hosts` |
| Aprobaciones humanas, idempotencia, recuperación, deduplicación, tareas programadas | `tests/test_agent.py`, `tests/test_channels.py` |
| Telegram (polling y webhook) contra una Bot API local | `test_end_to_end_telegram_run_once`, `test_webhook_server_end_to_end` |
| Total | **74/74 pruebas deterministas**, local y en GitHub Actions — `reports/pytest_resultados.txt` |
| CI en GitHub Actions (`flynnvertical`) | Corridas 41694120442 / 41695074464: job `pruebas` verde |
| Sandbox sin red también en runners de GitHub (`sudo unshare -n` + `setpriv`) | prueba obligatoria en CI, verde |
| **Evaluación en vivo con modelo real: 14/14 (100 %)** | run 41758915072 — `docs/PRUEBAS.md` |
| Conmutación automática Groq → OpenRouter ante cuota agotada | misma corrida: 9 llamadas Groq + 32 OpenRouter |
| Memoria entre dos máquinas (estado cifrado vía git) | `persistencia-1/2` ✔ en la misma corrida |
| Bot de Telegram con token válido | `@AgenteSupremoflynn_bot` (`getMe` en CI) |

## Construido pero NO comprobado en vivo
| Qué | Por qué | Qué hace falta |
|---|---|---|
| Bot respondiendo en Telegram | El cron sólo corre desde la rama por defecto | `main` como rama por defecto y `/vincular <código>` |
| Ejecución desatendida 24/7 | No desplegado: falta bot de Telegram, clave de modelo y merge | Opción A, B o C de `docs/INSTALACION.md` |
| Workflow `agente.yml` (estado en rama `agent-state`) | Los cron sólo corren desde la rama por defecto (hoy `claude/instalarlo-3wdb8j`) | Merge a `main` + `main` como rama por defecto |
| Búsqueda DuckDuckGo desde servidores | Puede ser bloqueada en IPs de nube | Medir en vivo; SearXNG/Brave como alternativa |
| Instalación en Termux | No hay dispositivo Android en la sesión | Probar `scripts/termux_instalar.sh` |

**No se afirma que el sistema funcione 24/7**: no se pudo comprobar ninguna ejecución desatendida.

## Faltan (próximas etapas, por utilidad)
2. Navegador con JavaScript (Playwright) para sitios dinámicos y portales de compras públicas.
3. Módulo licitaciones: conectores a portales oficiales (p. ej. COMPR.AR, Mercado Público) y plantilla de cotización auditable.
4. OCR para PDF escaneados.
5. Búsqueda semántica con embeddings (hoy BM25/FTS5, suficiente para memoria personal).
6. Exponer herramientas vía MCP para reutilizarlas desde otros clientes.

## Motores de modelo (estado real de las claves)
| Orden | Motor | Secreto | Estado |
|---|---|---|---|
| 1 | Groq | `GROQ_API_KEY` | ✅ cargado |
| 2 | OpenRouter `:free` | `OPENROUTER_API_KEY` | ✅ cargado |
| 3 | Gemini (AI Studio) | `GEMINI_API_KEY` | ❌ **falta**: nunca se generó ni se compartió en la sesión de instalación. Debe generarla el dueño (Mariano) en aistudio.google.com → *Get API key*, sin activar facturación, y cargarla él mismo en Settings → Secrets → Actions. |
| 4 | Mistral | `MISTRAL_API_KEY` | ❌ **mejora futura**: la cuenta nunca se creó; no se crean cuentas nuevas sin decisión del dueño. |

Cadena efectiva hoy: **Groq → OpenRouter** (2 motores). El código ya soporta los 4: basta con cargar la
clave para que el motor se sume a la cadena en la siguiente corrida, sin cambios de código.

## Operación
- Cron del agente: **cada 5 min** (mínimo de GitHub), con concurrencia `agente-estado` sin solapamientos.
- El registro de cada corrida informa `sent` (todos los mensajes enviados en la pasada), `send_failed`,
  `outbox_pending` y, si hubo, `send_error` (descripción de la API, sin contenido de mensajes).
- La evaluación en vivo corre sólo cuando cambia el razonamiento del agente o a mano, para no gastar
  el cupo diario gratuito del bot de producción.
