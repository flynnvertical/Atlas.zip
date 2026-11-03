"""Cliente LLM agnóstico de proveedor (API compatible con OpenAI Chat Completions).

Proveedores con cuota gratuita soportados (todos opcionales, en cadena de respaldo):
  github     -> GitHub Models, usa GITHUB_TOKEN (en Actions: permissions: models: read)
  groq       -> GROQ_API_KEY
  gemini     -> GEMINI_API_KEY (endpoint compatible OpenAI de Google AI Studio)
  openrouter -> OPENROUTER_API_KEY (modelos ':free')
  ollama     -> OLLAMA_BASE_URL (local, p. ej. en una PC propia)
Ninguno se activa con facturación: si la cuota se agota, el proveedor devuelve 429 y se
pasa al siguiente; si todos fallan, el trabajo queda en cola para la próxima ejecución.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .db import DB
from .security import redact


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """Se alcanzó el tope diario propio o todos los proveedores devolvieron 429."""


@dataclass
class Provider:
    name: str
    url: str
    model: str
    key: str | None
    extra_headers: dict | None = None


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v else default


def providers_from_env() -> list[Provider]:
    order = [x.strip() for x in (_env("AGENTE_LLM_PROVIDERS", "github,groq,gemini,openrouter,ollama") or "").split(",") if x.strip()]
    out: list[Provider] = []
    for name in order:
        if name == "github" and (_env("GITHUB_MODELS_TOKEN") or _env("GITHUB_TOKEN")):
            out.append(Provider("github", "https://models.github.ai/inference/chat/completions",
                                _env("GITHUB_MODELS_MODEL", "openai/gpt-4.1-mini"),
                                _env("GITHUB_MODELS_TOKEN") or _env("GITHUB_TOKEN")))
        elif name == "groq" and _env("GROQ_API_KEY"):
            out.append(Provider("groq", "https://api.groq.com/openai/v1/chat/completions",
                                _env("GROQ_MODEL", "llama-3.3-70b-versatile"), _env("GROQ_API_KEY")))
        elif name == "gemini" and _env("GEMINI_API_KEY"):
            out.append(Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                                _env("GEMINI_MODEL", "gemini-2.5-flash"), _env("GEMINI_API_KEY")))
        elif name == "openrouter" and _env("OPENROUTER_API_KEY"):
            out.append(Provider("openrouter", "https://openrouter.ai/api/v1/chat/completions",
                                _env("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
                                _env("OPENROUTER_API_KEY")))
        elif name == "ollama" and _env("OLLAMA_BASE_URL"):
            out.append(Provider("ollama", _env("OLLAMA_BASE_URL").rstrip("/") + "/v1/chat/completions",
                                _env("OLLAMA_MODEL", "qwen2.5:7b"), None))
    return out


class LLM:
    """Interfaz: chat(messages, tools=None, json_mode=False) -> dict mensaje asistente."""

    def __init__(self, db: DB | None, providers: list[Provider] | None = None, daily_cap: int = 120,
                 timeout: int = 90, sleep: Callable[[float], None] = time.sleep):
        self.db = db
        self.providers = providers if providers is not None else providers_from_env()
        self.daily_cap = daily_cap
        self.timeout = timeout
        self.sleep = sleep
        self.last_provider: str | None = None

    # -- contabilidad de uso ---------------------------------------------------
    def _today(self) -> str:
        return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")

    def calls_today(self) -> int:
        if not self.db:
            return 0
        r = self.db.one("SELECT COALESCE(SUM(calls),0) c FROM usage WHERE day=?", (self._today(),))
        return int(r["c"])

    def _record(self, provider: str, usage: dict | None, error: bool = False) -> None:
        if not self.db:
            return
        u = usage or {}
        self.db.execute(
            "INSERT INTO usage(day,provider,calls,prompt_tokens,completion_tokens,errors) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(day,provider) DO UPDATE SET calls=calls+excluded.calls, "
            "prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
            "completion_tokens=completion_tokens+excluded.completion_tokens, errors=errors+excluded.errors",
            (self._today(), provider, 0 if error else 1, int(u.get("prompt_tokens") or 0),
             int(u.get("completion_tokens") or 0), 1 if error else 0),
        )

    # -- llamada ---------------------------------------------------------------
    def chat(self, messages: list[dict], tools: list[dict] | None = None, json_mode: bool = False,
             temperature: float = 0.2) -> dict:
        if not self.providers:
            raise LLMError("No hay proveedor LLM configurado (ver docs/CONFIGURACION.md).")
        if self.calls_today() >= self.daily_cap:
            raise QuotaExhausted(f"Tope diario propio alcanzado ({self.daily_cap} llamadas).")
        errors: list[str] = []
        all_429 = True
        for p in self.providers:
            body: dict[str, Any] = {"model": p.model, "messages": messages, "temperature": temperature}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            headers = {"Content-Type": "application/json"}
            if p.key:
                headers["Authorization"] = f"Bearer {p.key}"
            for attempt in range(2):
                try:
                    r = requests.post(p.url, headers=headers, data=json.dumps(body), timeout=self.timeout)
                except requests.RequestException as e:
                    all_429 = False
                    errors.append(f"{p.name}: red {type(e).__name__}")
                    self._record(p.name, None, error=True)
                    self.sleep(2 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        all_429 = False
                        ctype = r.headers.get("Content-Type", "?")
                        errors.append(f"{p.name}: HTTP 200 sin JSON (Content-Type={ctype}; "
                                      f"inicio={redact(r.text[:160])!r})")
                        self._record(p.name, None, error=True)
                        break
                    try:
                        msg = data["choices"][0]["message"]
                    except (KeyError, IndexError):
                        errors.append(f"{p.name}: respuesta sin choices")
                        all_429 = False
                        break
                    self._record(p.name, data.get("usage"))
                    self.last_provider = f"{p.name}:{p.model}"
                    msg.setdefault("content", None)
                    return msg
                self._record(p.name, None, error=True)
                detail = redact(r.text[:300])
                errors.append(f"{p.name}: HTTP {r.status_code} {detail}")
                if r.status_code == 429:
                    break  # cuota: pasar al siguiente proveedor sin insistir
                all_429 = False
                if r.status_code in (500, 502, 503, 504):
                    self.sleep(3 * (attempt + 1))
                    continue
                if r.status_code == 400 and json_mode and "response_format" in body:
                    body.pop("response_format")  # algunos modelos no soportan json_mode
                    continue
                break  # 401/403/404/400: error de configuración, siguiente proveedor
        if all_429:
            raise QuotaExhausted("Todos los proveedores devolvieron 429: " + " | ".join(errors))
        raise LLMError("Fallaron todos los proveedores: " + " | ".join(errors))


class ScriptedLLM:
    """LLM determinista para pruebas: devuelve respuestas predefinidas en orden.

    Cada elemento puede ser un dict (mensaje), un str (contenido) o un callable(messages)->dict.
    Sólo se usa en pruebas; nunca se presenta como ejecución real de un modelo.
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.last_provider = "scripted"

    def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        self.calls.append(json.loads(json.dumps(messages)))
        if not self.script:
            raise LLMError("ScriptedLLM: guion agotado")
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            return {"role": "assistant", "content": item}
        return dict(item)


def tool_call(name: str, args: dict, call_id: str = "c1") -> dict:
    """Ayuda para construir mensajes con llamadas a herramientas (pruebas)."""
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}
