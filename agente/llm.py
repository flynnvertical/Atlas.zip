"""Cliente LLM agnóstico de proveedor (API compatible con OpenAI Chat Completions).

Proveedores con cuota gratuita soportados (todos opcionales, en cadena de respaldo):
  groq       -> GROQ_API_KEY
  gemini     -> GEMINI_API_KEY (endpoint compatible OpenAI de Google AI Studio)
  openrouter -> OPENROUTER_API_KEY (modelos ':free'; sin comprar créditos: 50 pedidos/día, 20/min)
  mistral    -> MISTRAL_API_KEY (modo gratuito, sin tarjeta)
  (Cerebras se evaluó y se descartó: desde 2026 su prueba gratuita exige cargar un medio de pago.)
  ollama     -> OLLAMA_BASE_URL (local, p. ej. en una PC propia)
  github     -> NO usar: GitHub Models fue retirado el 2026-07-30 (el endpoint responde "OK" en texto
                plano). Sólo se activa si se pide explícitamente en AGENTE_LLM_PROVIDERS.
Ninguno se activa con facturación: si la cuota se agota, el proveedor devuelve 429 y se
pasa al siguiente; si todos fallan, el trabajo queda en cola para la próxima ejecución.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .db import DB
from .security import redact


class LLMError(Exception):
    pass


class QuotaExhausted(LLMError):
    """Se alcanzó el tope diario propio o todos los proveedores devolvieron 429.

    retry_after: segundos sugeridos por el proveedor (None si no lo indicó)."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


_RA_RX = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


def retry_after_seconds(r) -> float | None:
    """Lee Retry-After (cabecera) o el texto 'try again in 1m2.5s' que usan Groq/OpenAI."""
    hdrs = r.headers if hasattr(r, "headers") else {}
    h = hdrs.get("retry-after")
    if h:
        try:
            return max(0.0, float(h))
        except ValueError:
            pass
    reset = hdrs.get("x-ratelimit-reset")  # OpenRouter: instante de reinicio en ms desde epoch
    if reset:
        try:
            v = float(reset)
            v = v / 1000 if v > 1e11 else v
            if v > 1e9:
                return max(0.0, v - time.time())
        except ValueError:
            pass
    m = _RA_RX.search(getattr(r, "text", "") or "")
    if m and any(m.groups()):
        hh, mm, ss = (float(x) if x else 0.0 for x in m.groups())
        return hh * 3600 + mm * 60 + ss
    return None


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


GROQ_DEFAULT_MODELS = "openai/gpt-oss-120b,llama-3.3-70b-versatile,openai/gpt-oss-20b"


def providers_from_env() -> list[Provider]:
    order = [x.strip() for x in (_env("AGENTE_LLM_PROVIDERS", "groq,openrouter,gemini,mistral,ollama") or "").split(",") if x.strip()]
    out: list[Provider] = []
    for name in order:
        if name == "github" and _env("GITHUB_MODELS_TOKEN"):  # retirado; sólo opt-in explícito
            out.append(Provider("github", "https://models.github.ai/inference/chat/completions",
                                _env("GITHUB_MODELS_MODEL", "openai/gpt-4.1-mini"),
                                _env("GITHUB_MODELS_TOKEN")))
        elif name == "groq" and _env("GROQ_API_KEY"):
            # Varios modelos en cadena: en Groq cada modelo tiene su propia cuota gratuita.
            models = [m.strip() for m in (_env("GROQ_MODEL", GROQ_DEFAULT_MODELS) or "").split(",") if m.strip()]
            for m in models:
                out.append(Provider("groq" if len(models) == 1 else f"groq/{m.split('/')[-1]}",
                                    "https://api.groq.com/openai/v1/chat/completions", m, _env("GROQ_API_KEY")))
        elif name == "gemini" and _env("GEMINI_API_KEY"):
            out.append(Provider("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                                _env("GEMINI_MODEL", "gemini-flash-latest"), _env("GEMINI_API_KEY")))
        elif name == "openrouter" and _env("OPENROUTER_API_KEY"):
            out.append(Provider("openrouter", "https://openrouter.ai/api/v1/chat/completions",
                                _env("OPENROUTER_MODEL", "openrouter/free"),
                                _env("OPENROUTER_API_KEY")))
        elif name == "mistral" and _env("MISTRAL_API_KEY"):
            out.append(Provider("mistral", "https://api.mistral.ai/v1/chat/completions",
                                _env("MISTRAL_MODEL", "mistral-small-latest"), _env("MISTRAL_API_KEY")))
        elif name == "ollama" and _env("OLLAMA_BASE_URL"):
            out.append(Provider("ollama", _env("OLLAMA_BASE_URL").rstrip("/") + "/v1/chat/completions",
                                _env("OLLAMA_MODEL", "qwen2.5:7b"), None))
    return out


class LLM:
    """Interfaz: chat(messages, tools=None, json_mode=False) -> dict mensaje asistente."""

    def __init__(self, db: DB | None, providers: list[Provider] | None = None, daily_cap: int = 120,
                 timeout: int = 90, sleep: Callable[[float], None] = time.sleep, max_inline_wait: float = 60):
        self.db = db
        self.providers = providers if providers is not None else providers_from_env()
        self.daily_cap = daily_cap
        self.timeout = timeout
        self.sleep = sleep
        self.max_inline_wait = max_inline_wait  # espera corta ante límites por minuto
        self.last_provider: str | None = None
        self.dead: set[str] = set()  # modelos inexistentes/retirados: no se reintentan en este proceso
        self._cool: dict[str, float] = {}  # enfriamiento por motor tras un 429 (también persistido en DB)
        self.short_wait = 8.0  # sólo se espera en el mismo motor si el límite vence en segundos

    # -- enfriamiento por motor -----------------------------------------------------
    def cooldown_until(self, name: str) -> float:
        until = self._cool.get(name, 0.0)
        if self.db:
            try:
                until = max(until, float(self.db.kv_get(f"cooldown:{name}", "0") or 0))
            except ValueError:
                pass
        return until

    def _set_cooldown(self, name: str, seconds: float) -> None:
        until = time.time() + max(1.0, seconds)
        self._cool[name] = until
        if self.db:
            self.db.kv_set(f"cooldown:{name}", str(until))

    def available(self) -> list[Provider]:
        now = time.time()
        return [p for p in self.providers if p.name not in self.dead and self.cooldown_until(p.name) <= now]

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
            raise LLMError("No hay proveedor LLM configurado: definí GROQ_API_KEY, GEMINI_API_KEY u OPENROUTER_API_KEY (ver docs/INSTALACION.md).")
        if self.calls_today() >= self.daily_cap:
            raise QuotaExhausted(f"Tope diario propio alcanzado ({self.daily_cap} llamadas).")
        errors: list[str] = []
        all_429 = True
        waits: list[float] = []
        avail = self.available()
        if not avail:
            live = [p for p in self.providers if p.name not in self.dead]
            if not live:
                raise LLMError("Ningún motor disponible (modelos inexistentes o mal configurados).")
            wait = min(self.cooldown_until(p.name) for p in live) - time.time()
            raise QuotaExhausted("Todos los motores están en espera por cuota (429).", retry_after=max(1.0, wait))
        for idx, p in enumerate(avail):
            others_left = idx < len(avail) - 1
            body: dict[str, Any] = {"model": p.model, "messages": messages, "temperature": temperature}
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"
                if p.name.startswith("openrouter"):
                    # Sólo enrutar a modelos gratuitos que soporten herramientas.
                    body["provider"] = {"require_parameters": True}
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
                    ra = retry_after_seconds(r)
                    # Con otro motor disponible se conmuta enseguida (salvo esperas de segundos);
                    # si es el último, se espera hasta max_inline_wait.
                    limit = self.short_wait if others_left else self.max_inline_wait
                    if ra is not None and ra <= limit and attempt == 0:
                        self.sleep(ra + 0.5)
                        continue
                    wait = ra if ra is not None else 300.0
                    self._set_cooldown(p.name, wait)
                    waits.append(wait)
                    break  # cuota: conmutar al siguiente motor
                all_429 = False
                if r.status_code in (500, 502, 503, 504):
                    self.sleep(3 * (attempt + 1))
                    continue
                if r.status_code == 400 and "tool_use_failed" in r.text and attempt == 0:
                    continue  # el modelo generó una llamada mal formada: un reintento
                if r.status_code == 400 and json_mode and "response_format" in body:
                    body.pop("response_format")  # algunos modelos no soportan json_mode
                    continue
                if r.status_code == 404 or "model_not_found" in r.text or "decommissioned" in r.text:
                    self.dead.add(p.name)
                if r.status_code == 402:  # OpenRouter: saldo negativo o sin crédito -> fuera por 1 h
                    self._set_cooldown(p.name, 3600)
                break  # 401/403/404/400: error de configuración, siguiente proveedor
        if all_429 or waits:
            # Si algún proveedor sólo está limitado por cuota, conviene esperar y no gastar intentos.
            raise QuotaExhausted("Cuota agotada (429) en los proveedores disponibles: " + " | ".join(errors),
                                 retry_after=min(waits) if waits else None)
        raise LLMError("Fallaron todos los proveedores: " + " | ".join(errors))


class ScriptedLLM:
    """LLM determinista para pruebas: devuelve respuestas predefinidas en orden.

    Cada elemento puede ser un dict (mensaje), un str (contenido) o un callable(messages)->dict.
    Sólo se usa en pruebas; nunca se presenta como ejecución real de un modelo.
    """

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[list[dict]] = []
        self.tools_sent: list[bool] = []
        self.last_provider = "scripted"

    def chat(self, messages, tools=None, json_mode=False, temperature=0.2):
        self.calls.append(json.loads(json.dumps(messages)))
        self.tools_sent.append(bool(tools))
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
