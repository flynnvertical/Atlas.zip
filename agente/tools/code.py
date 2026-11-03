"""Ejecución de código Python aislada y cálculo seguro."""
from __future__ import annotations

import ast
import math
import operator
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..security import redact, safe_env_for_subprocess
from . import CODE, READ, Tool, ToolContext, ToolError

_ISOLATION: bool | None = None


def network_isolation_available() -> bool:
    """Comprueba si se puede ejecutar en un namespace de red vacío (sin internet)."""
    global _ISOLATION
    if _ISOLATION is None:
        if not shutil.which("unshare"):
            _ISOLATION = False
        else:
            try:
                r = subprocess.run(["unshare", "-rn", sys.executable, "-c", "print(1)"],
                                   capture_output=True, timeout=10)
                _ISOLATION = r.returncode == 0 and r.stdout.strip() == b"1"
            except (OSError, subprocess.SubprocessError):
                _ISOLATION = False
    return _ISOLATION


def _limits(cpu: int, mem_mb: int):
    def apply():
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
        resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (50 * 1024 * 1024,) * 2)
    return apply


def run_python(ctx: ToolContext, code: str, timeout: int = 60) -> str:
    timeout = max(5, min(int(timeout), 120))
    ctx.workspace.mkdir(parents=True, exist_ok=True)
    script = ctx.workspace / f"_script_{int(time.time() * 1000)}.py"
    script.write_text(code, encoding="utf-8")
    isolated = network_isolation_available() and not ctx.settings.sandbox_network
    ctx.sandbox_isolated = isolated
    cmd =(["unshare", "-rn"] if isolated else []) + [sys.executable, "-I", str(script.name)]
    before = {p for p in ctx.workspace.rglob("*") if p.is_file()}
    t0 = time.time()
    try:
        r = subprocess.run(cmd, cwd=ctx.workspace, capture_output=True, timeout=timeout,
                           env=safe_env_for_subprocess(), preexec_fn=_limits(timeout, 1536))
        out, err, code_ret = r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace"), r.returncode
    except subprocess.TimeoutExpired:
        raise ToolError(f"tiempo agotado ({timeout}s)")
    finally:
        script.unlink(missing_ok=True)
    elapsed = time.time() - t0
    new_files = []
    for p in ctx.workspace.rglob("*"):
        if p.is_file() and p not in before and not p.name.startswith("_script_"):
            rel = str(p.relative_to(ctx.workspace))
            new_files.append(rel)
            if rel not in ctx.created_files:
                ctx.created_files.append(rel)
    res = (f"exit={code_ret} | {elapsed:.1f}s | red={'BLOQUEADA' if isolated else 'disponible'}\n"
           f"--- stdout ---\n{out[-6000:]}\n")
    if err.strip():
        res += f"--- stderr ---\n{err[-3000:]}\n"
    if new_files:
        res += "Archivos nuevos: " + ", ".join(new_files)
    return redact(res)


def _code_policy(ctx: ToolContext, args: dict) -> str | None:
    if not network_isolation_available() and ctx.tainted:
        return ("ejecución de código sin aislamiento de red después de leer contenido externo "
                "(riesgo de exfiltración por inyección)")
    return None


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {k: getattr(math, k) for k in ("sqrt", "log", "log10", "exp", "sin", "cos", "tan", "ceil", "floor")}
_FUNCS.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ToolError("exponente demasiado grande")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS:
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    if isinstance(node, ast.Name) and node.id in ("pi", "e"):
        return getattr(math, node.id)
    raise ToolError(f"expresión no permitida: {ast.dump(node)[:60]}")


def calculate(ctx: ToolContext, expression: str) -> str:
    try:
        tree = ast.parse(expression.replace("^", "**"), mode="eval")
    except SyntaxError as e:
        raise ToolError(f"sintaxis inválida: {e}") from e
    val = _eval(tree)
    return f"{expression} = {val}"


TOOLS = [
    Tool("run_python", "Ejecuta Python 3 en un entorno aislado (sin internet si es posible, sin secretos, "
         "límite de CPU/memoria). Directorio actual = espacio de trabajo del trabajo; los archivos que cree "
         "se adjuntan. Disponibles: pandas, openpyxl, python-docx, pypdf. Imprimí los resultados.",
         {"type": "object", "properties": {"code": {"type": "string"}, "timeout": {"type": "integer", "default": 60}},
          "required": ["code"]}, run_python, CODE, approval_policy=_code_policy),
    Tool("calculate", "Calculadora exacta y segura (+ - * / ** % sqrt log round min max sum).",
         {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]},
         calculate, READ),
]
