# Arquitectura de AGENTE SUPREMO

## Decisión principal: agente único propio + GitHub Actions + Telegram

```
 Android (Telegram)                    GitHub (repo público, USD 0)
 ┌──────────────┐  mensajes   ┌───────────────────────────────────────────────┐
 │  Tu chat con │ ──────────► │ Workflow "agente.yml" (cron cada 5 min o      │
 │  el bot      │ ◄────────── │ ejecución manual)                             │
 └──────────────┘ resultados, │  1. restaura estado cifrado (rama agent-state)│
                  archivos,   │  2. lee mensajes nuevos (getUpdates)          │
                  aprobaciones│  3. encola trabajos / ejecuta comandos        │
                              │  4. orquestador: plan → herramientas →        │
                              │     verificación → entrega                    │
                              │  5. guarda estado cifrado si cambió           │
                              └──────────┬────────────────────────────────────┘
                                         │ API compatible OpenAI
                         Groq → respaldo Gemini / OpenRouter / Ollama (claves gratuitas propias)
```

### Módulos (`agente/`)
| Módulo | Función |
|---|---|
| `agent.py` | Orquestador: plan con criterios de éxito, bucle de herramientas con presupuesto, aprobaciones, verificación, autocorrección, reintentos |
| `verify.py` | Controles deterministas (URLs citadas realmente consultadas, archivos existentes, fuentes cuando hubo investigación) + auditor LLM independiente |
| `tools/web.py` | `web_search` (SearXNG/Brave opcionales → DuckDuckGo → Wikipedia), `fetch_url` (HTML/PDF/JSON), `download_file`, `http_request` |
| `tools/files.py` | Lectura PDF/DOCX/XLSX/CSV/JSON/TXT; generación DOCX (desde markdown), XLSX, CSV, JSON, MD, HTML |
| `tools/code.py` | `run_python` aislado (namespace de red vacío, sin secretos, límites CPU/memoria/tamaño) y `calculate` exacto |
| `tools/memtools.py` | Memoria, avisos de progreso, tareas programadas |
| `memory.py` | Memoria persistente SQLite FTS5 (BM25, insensible a acentos), con origen, confianza, correcciones con histórico, expiración y borrado |
| `scheduler.py` | Cola con *leases* (recuperación de trabajos interrumpidos), deduplicación, tareas recurrentes (`every:N`, `daily:HH:MM` con zona horaria) |
| `llm.py` | Cliente multi-proveedor con respaldo ante 429/5xx, contabilidad de tokens y tope diario propio |
| `security.py` | Anti-SSRF (también en redirecciones), detección y cuarentena de inyecciones, redacción de secretos, entorno sin secretos para subprocesos |
| `telegram.py` / `app.py` | Bot API, lista blanca de usuarios, comandos, cola de salida persistente con reintentos, modo `run-once` (cron) y `poll` (continuo) |
| `state_store.py` | Estado (base + espacio de trabajo) empaquetado y cifrado AES-256-GCM, clave derivada con scrypt |

## Por qué esta combinación (y no otras)

Investigación realizada el 2026-11-03 (fuentes al final). Criterio: capacidad real por costo, con
la restricción dura de USD 0/mes y administración desde Android.

| Opción | Evaluación | Decisión |
|---|---|---|
| **OpenClaw** | Agente personal completo con Telegram, pero requiere un host siempre encendido (VPS = pago) y reportes de configuración/mantenimiento difíciles; modo por defecto con acceso amplio al host. | Descartado como base: no cumple USD 0 sin servidor y su superficie de seguridad es grande. |
| **Hermes Agent** | Interesante por auto-generación de habilidades; fuentes contradictorias sobre su integración con Telegram; también necesita host permanente. | Descartado por ahora. Reevaluable si conseguís un host gratuito permanente. |
| **LangGraph / LangChain** | Muy bueno para grafos durables complejos, pero agrega dependencias y abstracciones que no aportan para un agente único con ~16 herramientas. | No usado. La persistencia/reanudación que aporta LangGraph se implementó con checkpoints en SQLite (≈150 líneas, probadas). |
| **OpenAI Agents SDK** | Primitivas limpias (agentes, herramientas, guardrails), pero orientado a la API de OpenAI (paga). | No usado. Se adoptó el mismo formato de *function calling*, compatible con todos los proveedores gratuitos. |
| **Deep Agents** | Patrón planificador + subagentes + sistema de archivos. | Patrón adoptado parcialmente (plan + espacio de trabajo); sin subagentes porque multiplican llamadas y la cuota gratuita es el recurso escaso. |
| **n8n** | Excelente para integraciones, pero autoalojarlo exige servidor; la nube es paga. | No usado. Integrable a futuro vía `http_request` (con aprobación). |
| **MCP** | Estándar para conectar herramientas. | No es necesario para el MVP (las herramientas son internas). Ver “Próximos pasos”. |
| **Infraestructura** | GitHub Actions: minutos ilimitados en repos públicos, cron mínimo 5 min con demoras, desactivación tras 60 días sin actividad. Telegram Bot API: gratis. ~~GitHub Models~~ (retirado 2026-07-30): el modelo gratuito pasa a Groq/Gemini/OpenRouter con clave propia. | **Elegido**: es la única combinación que permite ejecución desatendida, persistencia y un LLM sin tarjeta de crédito. |

**Un solo agente**: con cuotas de ~50–150 pedidos/día, cada subagente adicional reduce la cantidad de
tareas útiles. El único "especialista" separado es el **auditor**, que sólo corre cuando los controles
deterministas ya pasaron, y su utilidad se comprueba en las pruebas (detecta respuestas que cumplen el
formato pero no el criterio).

## Modelo de seguridad
1. **Lista blanca**: sólo los IDs de Telegram en `TELEGRAM_ALLOWED_USERS` pueden dar órdenes.
2. **Aprobación humana obligatoria** (no la decide el modelo, la impone el código) para: borrar archivos
   o memorias, cualquier `POST/PUT/PATCH/DELETE` a servicios externos y ejecución de código sin
   aislamiento de red después de leer contenido externo. Cada aprobación muestra la acción y sus
   argumentos exactos, se ejecuta una sola vez y sólo la puede decidir el chat dueño del trabajo.
3. **Inyección de instrucciones**: el contenido web y de documentos se entrega al modelo como datos
   marcados; se detectan patrones de inyección y se alerta. Aun si el modelo fuese engañado, las
   acciones peligrosas siguen requiriendo tu aprobación.
4. **Código generado** (`run_python`): doble aislamiento verificado con intentos reales antes de usarse.
   *Red*: namespace vacío (`unshare -rn`; en runners de GitHub `sudo unshare -n` + `setpriv` para volver
   al usuario normal). *Archivos*: Landlock (`agente/sandbox_boot.py`) — lectura sólo del sistema y del
   intérprete, escritura sólo en el espacio del trabajo, carpeta compartida de sólo lectura, sin acceso
   a la base del agente ni a `/proc` (evita leer el entorno, con secretos, del proceso padre), sin TCP y
   sin señales a otros procesos. Además: sin variables secretas y con límites de CPU, memoria y tamaño.
   Si el host no ofrece Landlock (p. ej. Android/Termux), **toda** ejecución de código pide aprobación.
5. **SSRF**: se bloquean destinos internos (localhost, redes privadas, metadatos de nube), también tras
   redirecciones.
6. **Secretos**: sólo en GitHub Secrets; se redactan de salidas, mensajes y errores; los logs de Actions
   (públicos en repos públicos) sólo muestran métricas.
7. **Estado**: cifrado AES-256-GCM autenticado; sin `AGENTE_STATE_KEY` es ilegible aunque el repo sea público.

## Fuentes consultadas en la investigación
- **GitHub Models fue retirado el 2026-07-30** (inferencia no disponible para nadie): https://github.blog/changelog/2026-07-30-github-models-is-now-retired/ · aviso previo: https://github.blog/changelog/2026-07-01-github-models-is-being-fully-retired-on-july-30-2026/ · reporte del efecto en un workflow: https://github.com/qte77/gha-ai-changelog/issues/45
- Comparativas de APIs gratuitas vigentes (oct. 2026): https://openrouter.ai/blog/tutorials/free-llm-apis-compared/ · https://agentdeals.dev/free-llm-apis
- (Histórico, desactualizado) GitHub Models, facturación: https://docs.github.com/en/billing/concepts/product-billing/github-models
- Límites de GitHub Models (terceros, no oficiales): https://fast.io/resources/github-models-rate-limit/
- Permiso `models: read` en Actions (terceros): https://github.com/nsrosenqvist/nitpik/wiki/04-GitHub-Models
- Cuotas Groq (terceros): https://benchlm.ai/free-tier/groq · https://eesel.ai/blog/groq-pricing
- Cuotas Gemini (fuentes contradictorias): https://agentdeals.dev/gemini-api-pricing-changes · https://tinkerllm.com/blog/gemini-api-free-tier-limits-rate-quotas/
- Cron de Actions, 60 días y demoras: https://github.com/gautamkrishnar/keepalive-workflow · https://cronuru.com/guides/github-actions-scheduled-workflows
- OpenClaw vs Hermes: https://www.mindstudio.ai/blog/hermes-agent-vs-openclaw-self-hosted-ai-agent-comparison · https://use-apify.com/blog/openclaw-vs-hermes-agent-2026 · https://parallel.ai/articles/openclaw-vs-nousresearch-hermes.md

Las cifras de cuotas de terceros no coinciden entre sí: el sistema no depende de ninguna cifra
concreta. Controla su propio tope diario (`AGENTE_LLM_DAILY_CAP`) y, ante un 429, pausa y reanuda.

## Corrección registrada (2026-11-03)
La primera versión eligió **GitHub Models** como proveedor por defecto (gratis con `GITHUB_TOKEN`).
La investigación inicial se apoyó en páginas de terceros desactualizadas y no detectó que el servicio
había sido **retirado el 2026-07-30**. Se descubrió al ejecutar el CI real: el endpoint respondía
`HTTP 200 text/plain "OK"` a todo pedido. Corrección: GitHub Models salió de la cadena por defecto
(sólo se activa con `AGENTE_LLM_PROVIDERS=github` + `GITHUB_MODELS_TOKEN`), el cliente informa
respuestas no JSON y la evaluación en vivo exige una clave de Groq, Gemini u OpenRouter.

## Revisión de Codex en el PR #1 (corregida)
- **P1 sistema de archivos**: el código generado podía borrar/modificar archivos fuera de su espacio
  (incluida la base) sin aprobación → sandbox Landlock descrito arriba + aprobación obligatoria si no
  está disponible. Se detectó además la lectura de `/proc/<pid>/environ` del proceso padre (secretos).
- **P1 cuota**: en modo servidor el trabajador reintentaba en bucle con la cuota agotada y gastaba el
  presupuesto del trabajo → pausa persistente (5 min → 1 h exponencial ante 429; hasta la medianoche UTC
  con el tope propio), los intentos fallidos por cuota no cuentan y el trabajador espera.
- **P2 webhook**: el mensaje confirmado a Telegram vivía sólo en memoria → buzón SQLite escrito antes
  de responder 200 (503 si el trabajador no está vivo, para que Telegram reintente), procesado con
  deduplicación y sincronizado al estado cifrado apenas se recibe. Riesgo residual: en un host con
  disco efímero (Render) un reemplazo del contenedor en los segundos entre la recepción y la subida
  del estado puede perder ese mensaje.

## Segundo motor con conmutación automática (2026-11-04, pedido de Mariano)
Motivo: la evaluación en vivo se abortó por la cuota diaria gratuita de Groq; no se puede depender de
un solo proveedor. Cadena por defecto: **Groq → OpenRouter → Gemini → Mistral** (se usan los que
tengan clave). Evaluación de alternativas (USD 0, sin tarjeta):
- **OpenRouter** `:free` — elegido como secundario. Límite sin compra: 50 pedidos/día y 20/min; se
  pide `provider.require_parameters=true` para que sólo enrute a modelos gratuitos con herramientas.
  Fuentes: https://openrouter.zendesk.com/hc/en-us/articles/39501163636379-OpenRouter-Rate-Limits-What-You-Need-to-Know · https://costgoat.com/pricing/openrouter-free-models
- **Cerebras** — descartado: el nivel gratuito sin tarjeta fue reemplazado por un crédito de prueba que
  exige medio de pago. Fuentes: https://toolfreebie.com/cerebras-free-api/ · https://benchlm.ai/free-tier/cerebras
- **Gemini** (AI Studio) y **Mistral** ("Free mode", claves sin tarjeta) — soportados como respaldos
  adicionales; límites no publicados de forma estable. Fuentes: https://www.aifreeapi.com/en/posts/mistral-api-free-tier · https://geotoolbox.ai/blog/gemini-api-pricing.md

Mecanismo (`agente/llm.py`): enfriamiento por motor persistido en el estado (`cooldown:<motor>`),
conmutación inmediata ante 429/402, espera en el mismo motor sólo si el límite vence en ≤ 8 s, y
registro por trabajo de qué motor respondió (`counters.motores`, pie `⚙️ Motor:` y columna en la
evaluación en vivo). Pruebas: `tests/test_fallback.py`.
