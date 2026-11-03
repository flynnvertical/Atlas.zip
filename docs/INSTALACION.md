# Instalación y despliegue

Todo se puede hacer desde el celular (navegador + app de Telegram). Ningún paso requiere tarjeta
de crédito. **Nunca pegues claves en el chat con el agente ni en el código**: van en *Secrets*.

## Paso 0 — Bot de Telegram ✅ (creado: @AgenteSupremoflynn_bot)
El token es una credencial: va **sólo** en GitHub Secrets, nunca en el chat, el código o commits.
Si alguna vez lo pegaste en un chat, revocalo cuando el bot funcione (@BotFather → `/revoke`) y
cargá el nuevo directamente como secreto.

### Autorizar tu cuenta (elegí una)
- **A. Vinculación con código (no necesitás saber tu ID):** creá el secreto `AGENTE_PAIRING_CODE`
  con una frase de al menos 8 caracteres. Cuando el agente esté corriendo, escribile al bot en chat
  privado `/vincular <tu frase>`. El primer código correcto te registra como dueño (queda guardado en
  el estado cifrado) y la vinculación se cierra para siempre. 5 intentos fallidos la bloquean.
  Después podés borrar ese secreto.
- **B. Lista fija:** escribile `/id` al bot, te responde tu número; guardalo como secreto
  `TELEGRAM_ALLOWED_USERS` (varios IDs separados por coma). Si este secreto existe, la vinculación
  por código queda deshabilitada.

## Paso 1 — Proveedor de modelo (al menos uno, gratis)
| Proveedor | Cómo obtenerlo | Variable |
|---|---|---|
| Groq (recomendado) | console.groq.com → API Keys | `GROQ_API_KEY` |
| Gemini (respaldo) | aistudio.google.com → Get API key (sin activar facturación) | `GEMINI_API_KEY` |
| OpenRouter (respaldo) | openrouter.ai → Keys (modelos `:free`) | `OPENROUTER_API_KEY` |

Si configurás varios, se usan en cadena: ante una cuota agotada (HTTP 429) pasa al siguiente.

> **GitHub Models ya no sirve**: GitHub lo retiró el 2026-07-30 (su endpoint responde `OK` en
> texto plano). Por eso hace falta al menos una clave propia gratuita. Cargala como secreto del repo
> (Settings → Secrets and variables → Actions) con el nombre exacto de la tabla.

## Paso 2 — Elegí dónde corre

### Opción A — GitHub Actions (USD 0, desatendido, respuesta en ~10–30 min)
> Actions funciona desde la migración a `flynnvertical` (antes la cuenta `cargazul33` estaba bloqueada
> por facturación). Los cron sólo corren desde la **rama por defecto** del repo.


1. Repo → Settings → Secrets and variables → Actions → *New repository secret*:
   - `TELEGRAM_BOT_TOKEN` — el token del paso 0.
   - `TELEGRAM_ALLOWED_USERS` — tu ID (varios separados por coma).
   - `AGENTE_STATE_KEY` — una frase larga y aleatoria (≥ 16 caracteres; ideal 40+). **Si la perdés,
     perdés la memoria** (está cifrada con ella). Guardala en tu gestor de contraseñas.
   - `GROQ_API_KEY` (obligatoria salvo que uses `GEMINI_API_KEY` u `OPENROUTER_API_KEY`).
2. Fusioná el PR en `main` y hacé que `main` sea la rama por defecto (Settings → General → Default branch); hoy la rama por defecto es `claude/instalarlo-3wdb8j`.
3. Actions → *Agente (Telegram, ejecución programada)* → **Run workflow** para la primera prueba.
4. Escribile a tu bot. El cron corre cada 10 min (GitHub puede demorarlo).

### Opción B — Render plan gratuito (USD 0, respuesta en segundos; ~1 min si estaba dormido)
1. Creá un token *fine-grained* con permiso **Contents: Read and write** SÓLO sobre este repo
   (lo usa para guardar el estado cifrado en la rama `agent-state`).
2. render.com → New → **Blueprint** → elegí este repo (usa `render.yaml`).
3. Completá las variables marcadas: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`,
   `AGENTE_STATE_KEY`, `AGENTE_STATE_REPO` (= `cargazul33/Atlas.zip`), `AGENTE_STATE_GH_TOKEN`,
   `GROQ_API_KEY` (y respaldos opcionales). `TELEGRAM_WEBHOOK_SECRET` se genera solo.
4. Al arrancar, el servicio registra el webhook de Telegram automáticamente (usa `RENDER_EXTERNAL_URL`).
5. Limitaciones reales: se duerme a los 15 min sin tráfico; **las tareas programadas sólo corren
   mientras está despierto**. Para que corran, configurá un ping gratuito (p. ej. cron-job.org) a
   `https://<tu-servicio>.onrender.com/tick?k=<AGENTE_TICK_KEY>` cada 30–60 min (consume horas de las
   750 mensuales: 1 instancia 24/7 ≈ 720–744 h/mes, justo en el límite).

### Opción C — Android con Termux (USD 0, respuesta en segundos, sólo con el teléfono encendido)
1. Instalá **Termux desde F-Droid** (la versión de Play Store está desactualizada).
2. En Termux: `curl -O https://raw.githubusercontent.com/cargazul33/Atlas.zip/<rama>/scripts/termux_instalar.sh && bash termux_instalar.sh`
3. Editá `~/.agente.env` con `nano ~/.agente.env` y volvé a ejecutar el script.
4. Desactivá la optimización de batería para Termux. En Android no existe `unshare`, así que
   `run_python` pedirá tu aprobación después de leer contenido web (protección anti-exfiltración).
> No probado en un dispositivo real dentro de esta sesión: la instalación de `cryptography` y `lxml`
> en Termux puede requerir compilar (incluí `rust` y `binutils`).

### Opción D — PC propia (Windows/Mac/Linux)
```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_ALLOWED_USERS=... GROQ_API_KEY=...
python -m agente doctor --online
python -m agente poll         # o: python -m agente ask "tu objetivo" (sin Telegram)
```

## Variables de configuración
| Variable | Por defecto | Efecto |
|---|---|---|
| `AGENTE_LLM_PROVIDERS` | `groq,gemini,openrouter,ollama` | Orden de la cadena de proveedores |
| `GROQ_MODEL` / `GEMINI_MODEL` / `OPENROUTER_MODEL` | `openai/gpt-oss-120b` / `gemini-flash-latest` / `openrouter/free` | Modelo por proveedor (los catálogos cambian: verificá) |
| `AGENTE_LLM_DAILY_CAP` | 120 | Tope propio de llamadas/día (protege la cuota gratuita) |
| `AGENTE_MAX_LLM_CALLS` / `AGENTE_MAX_TOOL_CALLS` / `AGENTE_MAX_SECONDS` | 14 / 24 / 420 | Presupuesto por trabajo |
| `AGENTE_CONTEXT_CHARS` | 24000 | Tamaño máximo de contexto enviado (cuotas gratuitas ≈ 8k tokens/pedido) |
| `AGENTE_TZ` | America/Argentina/Buenos_Aires | Zona horaria de tareas diarias |
| `SEARXNG_URL` / `BRAVE_API_KEY` | — | Motores de búsqueda opcionales (si no: DuckDuckGo → Wikipedia) |
| `OLLAMA_BASE_URL` | — | Modelo local propio, sin cuotas |

## Recuperación ante fallas
| Falla | Qué pasa | Qué hacer |
|---|---|---|
| Se corta una ejecución a mitad de un trabajo | El *lease* vence (15 min) y el trabajo se reanuda desde su último checkpoint | Nada |
| Cuota del modelo agotada | Los trabajos quedan en cola; aviso una vez por día | Esperar al día siguiente o agregar un proveedor de respaldo |
| Telegram no disponible al enviar | El resultado queda en la cola de salida y se reintenta (5 veces) | `/ver N` para leerlo |
| Estado dañado / clave errónea | El agente se niega a arrancar con estado vacío (no pisa el bueno) | Restaurar una copia: artifacts `estado-cifrado-*` (Actions, 5 días) o historial de la rama `agent-state` |
| GitHub desactivó el cron (60 días) | El workflow intenta re-habilitarse en cada corrida; si ya está desactivado, no corre | Actions → workflow → *Enable workflow* |
| Perdiste `AGENTE_STATE_KEY` | El estado es irrecuperable (por diseño) | Nueva clave; se empieza con memoria vacía |
| Exportar / borrar todo | — | `/olvidar todo`; borrar la rama `agent-state` elimina el estado remoto |
