# Uso diario desde Android

La interfaz es tu chat con el bot de Telegram. Escribí objetivos en lenguaje natural:

- "Investigá los 5 principales importadores de cables de cobre en Chile, con fuentes."
- "Leé el pliego que te mando y armá una planilla con los ítems, cantidades y requisitos."  (adjuntá el PDF)
- "Calculá el margen si compro a 1.250 USD y vendo a 1.900 USD con 21% de IVA y 3% de comisión."
- "Generá un informe.docx con el resumen y una tabla comparativa."

El bot responde `📥 Trabajo #N en cola`, después (según la opción de despliegue: segundos o
10–30 min) envía el resultado y los archivos generados. Cada resultado termina con:
- `✅ Verificación superada` — pasaron los controles (fuentes realmente consultadas, archivos
  existentes, auditor conforme).
- `⚠️ Verificación NO superada` + motivos — usalo con cautela.

Las afirmaciones vienen marcadas como **[VERIFICADO: fuente]**, **[PROBABLE]** o **[NO CONFIRMADO]**.

## Comandos
| Comando | Uso |
|---|---|
| `/estado` | Trabajos activos, aprobaciones pendientes, consumo del día |
| `/tareas` · `/ver N` | Últimos trabajos · resultado completo del trabajo N |
| `/cancelar N` · `/reintentar N` | Cancelar / volver a ejecutar |
| `/aprobar N` · `/rechazar N` | Decidir acciones sensibles (borrar, enviar/publicar a servicios externos…) |
| `/recordar texto` | Guardar una preferencia o instrucción ("Siempre cotizá en USD") |
| `/memoria [búsqueda]` · `/olvidar N` · `/olvidar todo` | Ver y borrar memoria |
| `/programar daily:08:30 objetivo` | Tarea diaria (hora local) |
| `/programar every:180 objetivo` | Tarea cada N minutos (mínimo 60) |
| `/programas` · `/desprogramar N` | Ver / quitar tareas recurrentes |
| `/uso` | Consumo de cuota de modelos por día y proveedor |
| `/id` | Tu ID de Telegram |

## Aprobaciones
Cuando una acción puede tener efectos fuera del sistema, el bot envía:
```
🔐 Aprobación requerida #3 (trabajo #12)
Acción: http_request
Motivo: petición HTTP POST que puede modificar un servicio externo
Argumentos: {...}
Respondé /aprobar 3 o /rechazar 3
```
Revisá los argumentos: si el pedido aparece después de que el agente leyó una página web y no lo
esperabas, puede ser una inyección. Rechazalo.

## Administración desde el celular
- **App GitHub** (o el navegador): Actions → ver ejecuciones, *Run workflow* manual, editar secretos
  (Settings → Secrets) desde el navegador en modo escritorio.
- **Render** (si usás la opción B): dashboard web para logs y reinicios.
- **Termux** (opción C): `bash ~/Atlas.zip/scripts/termux_instalar.sh` para arrancar.
