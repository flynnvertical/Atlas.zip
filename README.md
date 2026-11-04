# AGENTE SUPREMO

Agente personal de IA que **ejecuta** tareas reales —investigar con fuentes, leer y generar documentos,
calcular, programar, recordar— y te entrega resultados verificados por **Telegram**, con costo
operativo de **USD 0** usando cuotas gratuitas.

```
Telegram ⇄ agente (plan → herramientas → verificación → entrega) ⇄ modelo gratuito (Groq / Gemini / OpenRouter / Ollama)
                          │
                 estado cifrado AES-256-GCM (memoria, trabajos, archivos) en la rama agent-state
```

## Qué incluye
- **Orquestador** que define criterios de éxito antes de ejecutar, usa herramientas con presupuesto
  duro, se reanuda tras interrupciones y verifica el resultado (controles deterministas + auditor).
- **16 herramientas**: búsqueda y lectura web, descarga de archivos, APIs, PDF/DOCX/XLSX/CSV/JSON,
  Python aislado sin red ni secretos, calculadora exacta, memoria, tareas recurrentes, avisos.
- **Memoria persistente** con búsqueda, correcciones con histórico y borrado.
- **Seguridad**: lista blanca de usuarios, aprobación humana obligatoria para acciones externas o
  irreversibles, anti-SSRF, cuarentena de inyecciones, redacción de secretos, estado cifrado.
- **3 formas de ejecutarlo gratis**: GitHub Actions (cron), Render (webhook) o Android/Termux.

## Empezar
1. [`docs/INSTALACION.md`](docs/INSTALACION.md) — crear el bot, elegir dónde corre, secretos.
2. [`docs/USO_ANDROID.md`](docs/USO_ANDROID.md) — uso diario y comandos.
3. [`docs/COSTOS_Y_LIMITES.md`](docs/COSTOS_Y_LIMITES.md) — qué es gratis y bajo qué condiciones.
4. [`docs/ARQUITECTURA.md`](docs/ARQUITECTURA.md) — diseño, alternativas evaluadas y seguridad.
5. [`docs/PRUEBAS.md`](docs/PRUEBAS.md) y [`docs/ESTADO.md`](docs/ESTADO.md) — qué está comprobado y qué falta.

## Desarrollo
```bash
pip install -r requirements-dev.txt
python -m pytest tests -q            # 72 pruebas deterministas
python -m agente doctor              # diagnóstico de configuración
python -m agente ask "objetivo"      # ejecutar sin Telegram (requiere un proveedor de modelo)
python scripts/live_eval.py          # evaluación en vivo con modelo e internet reales
```
