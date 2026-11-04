"""Registro de herramientas con niveles de riesgo y política de aprobación."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Niveles de riesgo
READ = "read"                # lectura local / cálculo
EXTERNAL = "external"        # lee internet (contenido no confiable -> "contamina" el trabajo)
LOCAL_WRITE = "local_write"  # escribe en el espacio de trabajo aislado
CODE = "code"                # ejecuta código generado
SIDE_EFFECT = "side_effect"  # efecto externo/irreversible -> SIEMPRE requiere aprobación humana


@dataclass
class ToolContext:
    settings: Any
    db: Any
    memory: Any
    job_id: int
    chat_id: int
    workspace: Path
    tainted: bool = False                       # leyó contenido externo no confiable
    seen_urls: set[str] = field(default_factory=set)
    created_files: list[str] = field(default_factory=list)
    notify: Callable[[str], None] | None = None
    sandbox_isolated: bool | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    func: Callable[..., str]
    risk: str = READ
    # Devuelve un motivo (str) si la llamada requiere aprobación; None si no.
    approval_policy: Callable[[ToolContext, dict], str | None] | None = None

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}

    def needs_approval(self, ctx: ToolContext, args: dict) -> str | None:
        if self.risk == SIDE_EFFECT:
            return "acción con efecto externo o irreversible"
        if self.approval_policy:
            return self.approval_policy(ctx, args)
        return None


class ToolError(Exception):
    pass


def args_hash(name: str, args: dict) -> str:
    return hashlib.sha256((name + json.dumps(args, sort_keys=True)).encode()).hexdigest()[:16]


def build_registry() -> dict[str, Tool]:
    from . import code, files, memtools, web
    reg: dict[str, Tool] = {}
    for mod in (web, files, code, memtools):
        for t in mod.TOOLS:
            reg[t.name] = t
    return reg
