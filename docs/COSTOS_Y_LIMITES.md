# Costos operativos y límites reales

**Costo objetivo y de diseño: USD 0/mes.** Ningún componente requiere tarjeta. Nada de esto es
"ilimitado": abajo están las condiciones. El agente no activa servicios pagos.

| Componente | Costo | Condiciones y límites |
|---|---|---|
| Código y librerías | 0 | Software libre (requests, bs4, lxml, pypdf, python-docx, openpyxl, cryptography) |
| Telegram Bot API | 0 | Límites de envío de Telegram (≈30 msg/s; archivos de bots hasta 50 MB de subida, 20 MB de descarga) |
| GitHub Actions (opción A) | 0 en repo **público** | Funciona en `flynnvertical` (verificado 2026-11-03). Cron mínimo 5 min y con demoras sin garantía; desactivación tras 60 días sin actividad; logs públicos (el agente sólo imprime métricas). Cron cada 5 min (≈8.600 corridas/mes, gratis sólo porque el repo es **público**). Si el repo pasara a privado: 2.000 min/mes gratis → hay que subir el intervalo a 30–60 min |
| ~~GitHub Models~~ | — | **Retirado por GitHub el 2026-07-30**; no usar |
| Groq (primario) | 0 | Por modelo: ~8.000 tokens/min y cupo diario de tokens en ventana móvil de 24 h (medido: ~2–3 evaluaciones completas por día) |
| OpenRouter `:free` (respaldo) | 0 **sin comprar créditos** | 50 pedidos/día y 20/min (con compra de USD 10 sube a 1.000/día: **no permitido** por la regla USD 0) |
| Gemini / Mistral (respaldo) | 0 | Límites por proyecto/organización, no publicados de forma estable; ver la consola de cada uno |
| ~~Cerebras~~ | — | Descartado: la prueba gratuita exige medio de pago |
| Render (opción B) | 0 | 750 h/mes por espacio de trabajo, se duerme a los 15 min, ~1 min para despertar, disco efímero, puede reiniciarse en cualquier momento |
| Termux (opción C) | 0 | Batería y datos móviles propios; sólo funciona con el teléfono encendido |

## Consumo por tarea (diseño, a medir en vivo)
| Tipo | Llamadas al modelo |
|---|---|
| Conversación simple | 2 (plan + respuesta) |
| Tarea con herramientas | 3–8 (plan + pasos + auditor) |
| Tope duro por trabajo | 14 (`AGENTE_MAX_LLM_CALLS`) |
| Tope diario propio | 120 (`AGENTE_LLM_DAILY_CAP`) |

Con ~1.000 pedidos/día (Groq, según fuentes de terceros que no coinciden) o 50/día (OpenRouter sin crédito) ⇒ entre **≈ 6 y 100+ tareas por día** según el proveedor. Medilo con `/uso`. Para más capacidad sin pagar: agregar
proveedores de respaldo (cada uno suma su cuota) o un modelo local con Ollama en una PC propia.
`/uso` muestra el consumo real por día y proveedor.

## Límites funcionales conocidos
- Sin navegador con JavaScript: páginas que requieren JS o login no se leen (sí HTML estático, PDF, JSON y APIs).
- La búsqueda gratuita (DuckDuckGo HTML) puede bloquear IPs de centros de datos; entonces cae a Wikipedia. Para búsquedas generales confiables: SearXNG propio o Brave Search API (`BRAVE_API_KEY`, plan gratuito con límite mensual).
- PDF escaneados: no hay OCR (se avisa).
- Latencia en opción A: cron cada 5 min (mínimo de GitHub); en la práctica ~5–20 min por respuesta según la demora de GitHub. No es un chat en tiempo real.
- No hay envío de correos ni compras: cualquier acción externa pasa por `http_request` con aprobación.
