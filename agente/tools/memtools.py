"""Herramientas de memoria, programación de tareas y avisos al usuario."""
from __future__ import annotations

from . import LOCAL_WRITE, READ, SIDE_EFFECT, Tool, ToolContext, ToolError


def memory_search(ctx: ToolContext, query: str, project: str = "") -> str:
    res = ctx.memory.search(query, limit=8, project=project or None)
    if not res:
        return "Sin memorias relevantes."
    return "\n".join(f"mem#{m['id']} [{m['kind']}{'/' + m['project'] if m['project'] else ''}; "
                     f"origen={m['source']}; confianza={m['confidence']}] {m['content']}" for m in res)


def memory_save(ctx: ToolContext, content: str, kind: str = "result", project: str = "",
                confidence: str = "media") -> str:
    mid = ctx.memory.save(content, kind=kind, project=project, source="agent",
                          confidence=confidence, chat_id=ctx.chat_id)
    return f"Guardado como mem#{mid} (origen=agent)."


def memory_correct(ctx: ToolContext, memory_id: int, new_content: str) -> str:
    try:
        nid = ctx.memory.correct(int(memory_id), new_content, source="agent")
    except KeyError as e:
        raise ToolError(str(e)) from e
    return f"mem#{memory_id} reemplazada por mem#{nid} (la anterior se conserva como histórico)."


def memory_delete(ctx: ToolContext, memory_id: int) -> str:
    ok = ctx.memory.delete(int(memory_id))
    return "Eliminada." if ok else "No existía."


def notify_user(ctx: ToolContext, text: str) -> str:
    if ctx.notify:
        ctx.notify(text[:1500])
        return "Aviso enviado."
    return "Sin canal de aviso disponible (se incluirá en el resultado final)."


def schedule_task(ctx: ToolContext, objective: str, spec: str) -> str:
    from ..scheduler import add_schedule
    try:
        sid = add_schedule(ctx.db, ctx.chat_id, objective, spec, ctx.settings.timezone)
    except ValueError as e:
        raise ToolError(str(e)) from e
    return f"Tarea recurrente #{sid} creada ({spec}). El usuario puede cancelarla con /desprogramar {sid}."


TOOLS = [
    Tool("memory_search", "Busca en la memoria persistente (preferencias, proyectos, resultados previos).",
         {"type": "object", "properties": {"query": {"type": "string"}, "project": {"type": "string"}},
          "required": ["query"]}, memory_search, READ),
    Tool("memory_save", "Guarda un dato útil para el futuro (breve, sin datos sensibles innecesarios). "
         "kind: preference|instruction|project|result|experience|fact.",
         {"type": "object", "properties": {
             "content": {"type": "string"}, "kind": {"type": "string"}, "project": {"type": "string"},
             "confidence": {"type": "string", "enum": ["baja", "media", "alta"]}},
          "required": ["content"]}, memory_save, LOCAL_WRITE),
    Tool("memory_correct", "Corrige una memoria existente (conserva histórico).",
         {"type": "object", "properties": {"memory_id": {"type": "integer"}, "new_content": {"type": "string"}},
          "required": ["memory_id", "new_content"]}, memory_correct, LOCAL_WRITE),
    Tool("memory_delete", "Elimina una memoria (requiere aprobación).",
         {"type": "object", "properties": {"memory_id": {"type": "integer"}}, "required": ["memory_id"]},
         memory_delete, SIDE_EFFECT),
    Tool("notify_user", "Envía un aviso de progreso breve al usuario por Telegram.",
         {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, notify_user, READ),
    Tool("schedule_task", "Programa una tarea recurrente. spec: 'every:<minutos>' (mín. 60) o 'daily:HH:MM' (hora local).",
         {"type": "object", "properties": {"objective": {"type": "string"}, "spec": {"type": "string"}},
          "required": ["objective", "spec"]}, schedule_task, LOCAL_WRITE),
]
