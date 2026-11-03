"""Ejecución de código Python aislada y cálculo seguro."""
from __future__ import annotations

import ast
import math
import operator
import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..security import redact, safe_env_for_subprocess
from . import CODE, READ, Tool, ToolContext, ToolError

_PREFIX: list[str] | None = None
_PROBE = ("import socket\ns=socket.socket();s.settimeout(3)\n"
          "try:\n    s.connect(('1.1.1.1',53));print('RED')\nexcept OSError:\n    print('SINRED')")


def _candidate_prefixes() -> list[list[str]]:
    """Formas de ejecutar sin red, de menor a mayor privilegio requerido."""
    cands = []
    if shutil.which("unshare"):
        cands.append(["unshare", "-rn"])  # namespace de usuario sin privilegios (Linux común)
        # Runners de GitHub: los namespaces sin privilegios están restringidos, pero hay sudo sin
        # contraseña. Se crea el namespace de red como root y se vuelve de inmediato al usuario normal.
        if os.geteuid() != 0 and shutil.which("sudo") and shutil.which("setpriv"):
            cands.append(["sudo", "-n", "unshare", "-n", "--", "setpriv", f"--reuid={os.getuid()}",
                          f"--regid={os.getgid()}", "--clear-groups", "--"])
    return cands


def isolation_prefix() -> list[str]:
    """Prefijo de comando que garantiza ejecución sin red, verificado con una conexión real."""
    global _PREFIX
    if _PREFIX is None:
        _PREFIX = []
        for pre in _candidate_prefixes():
            try:
                r = subprocess.run(pre + [sys.executable, "-I", "-c", _PROBE], capture_output=True, timeout=15)
            except (OSError, subprocess.SubprocessError):
                continue
            if r.returncode == 0 and r.stdout.strip() == b"SINRED":
                _PREFIX = pre
                break
    return _PREFIX


def network_isolation_available() -> bool:
    return bool(isolation_prefix())


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
    cmd = (isolation_prefix() if isolated else []) + [sys.executable, "-I", str(script.name)]
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
