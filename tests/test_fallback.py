"""Segundo motor con conmutación automática (pedido de Mariano, 2026-11-04)."""
import json
import time

import pytest

from agente.app import App
from agente.llm import LLM, Provider, QuotaExhausted, providers_from_env, retry_after_seconds
from agente.scheduler import enqueue

from .conftest import FakeMessenger, msg


def _ok(content="ok", tool=None):
    m = {"role": "assistant", "content": content}
    if tool:
        m = {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]}
    return 200, {"Content-Type": "application/json"}, json.dumps(
        {"choices": [{"message": m}], "usage": {"prompt_tokens": 5, "completion_tokens": 1}}).encode()


def _quota(seconds="3600"):
    return 429, {"Content-Type": "application/json", "retry-after": seconds}, b'{"error":"tokens per day"}'


class Engine:
    """Motor LLM falso con contador de pedidos y cuerpos recibidos."""

    def __init__(self, server, responses):
        self.responses = list(responses)
        self.bodies = []
        self.srv = server({"/v1/chat/completions": self.route})
        self.url = self.srv.url + "/v1/chat/completions"

    def route(self, method, path, body):
        self.bodies.append(json.loads(body))
        r = self.responses.pop(0) if self.responses else _ok()
        return r() if callable(r) else r


def test_switches_to_secondary_on_429_and_records_engine(server, db):
    groq = Engine(server, [_quota()])
    orouter = Engine(server, [_ok("respuesta del secundario")])
    llm = LLM(db, [Provider("groq", groq.url, "gpt-oss", "k"), Provider("openrouter", orouter.url, "openrouter/free", "k")],
              sleep=lambda s: pytest.fail("no debe esperar si hay otro motor"))
    assert llm.chat([{"role": "user", "content": "x"}])["content"] == "respuesta del secundario"
    assert llm.last_provider == "openrouter:openrouter/free"
    # el primario queda en enfriamiento: el siguiente pedido va directo al secundario
    llm.chat([{"role": "user", "content": "y"}])
    assert len(groq.bodies) == 1 and len(orouter.bodies) == 2


def test_cooldown_persists_across_processes(server, db):
    """Producción (cron/Telegram): una corrida nueva no vuelve a golpear al motor sin cuota."""
    groq = Engine(server, [_quota("1800")])
    sec = Engine(server, [])
    provs = [Provider("groq", groq.url, "m", "k"), Provider("openrouter", sec.url, "m", "k")]
    LLM(db, provs).chat([{"role": "user", "content": "x"}])
    LLM(db, provs).chat([{"role": "user", "content": "y"}])  # instancia nueva, misma base
    assert len(groq.bodies) == 1 and len(sec.bodies) == 2


def test_short_limit_waits_on_same_engine_instead_of_spending_secondary(server, db):
    groq = Engine(server, [_quota("3"), _ok("primario")])
    sec = Engine(server, [])
    waits = []
    llm = LLM(db, [Provider("groq", groq.url, "m", "k"), Provider("openrouter", sec.url, "m", "k")], sleep=waits.append)
    assert llm.chat([{"role": "user", "content": "x"}])["content"] == "primario"
    assert waits == [3.5] and sec.bodies == []  # no gasta los 50 pedidos/día del secundario


def test_all_engines_exhausted_raises_quota_with_soonest_retry(server, db):
    a = Engine(server, [_quota("600")])
    b = Engine(server, [_quota("120")])
    llm = LLM(db, [Provider("a", a.url, "m", "k"), Provider("b", b.url, "m", "k")], sleep=lambda s: None)
    with pytest.raises(QuotaExhausted) as e:
        llm.chat([{"role": "user", "content": "x"}])
    assert e.value.retry_after == pytest.approx(120, abs=2)


def test_openrouter_only_routes_to_tool_capable_models(server, db):
    orouter = Engine(server, [_ok()])
    llm = LLM(db, [Provider("openrouter", orouter.url, "openrouter/free", "k")])
    llm.chat([{"role": "user", "content": "x"}], tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}])
    assert orouter.bodies[0]["provider"] == {"require_parameters": True}


def test_openrouter_402_cools_engine(server, db):
    orouter = Engine(server, [(402, {"Content-Type": "application/json"}, b'{"error":"insufficient credits"}')])
    sec = Engine(server, [])
    llm = LLM(db, [Provider("openrouter", orouter.url, "m", "k"), Provider("mistral", sec.url, "m", "k")])
    llm.chat([{"role": "user", "content": "x"}])
    assert llm.cooldown_until("openrouter") > time.time() + 3000


def test_openrouter_reset_header_in_ms():
    class R:
        headers = {"x-ratelimit-reset": str(int((time.time() + 90) * 1000))}
        text = ""
    assert retry_after_seconds(R()) == pytest.approx(90, abs=2)


def test_default_engine_order(monkeypatch):
    for k in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY", "MISTRAL_API_KEY", "OLLAMA_BASE_URL",
              "AGENTE_LLM_PROVIDERS", "GITHUB_MODELS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GROQ_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("OPENROUTER_API_KEY", "o")
    monkeypatch.setenv("MISTRAL_API_KEY", "m")
    assert [p.name for p in providers_from_env()] == ["groq", "openrouter", "mistral"]


def test_production_job_falls_back_and_reports_engine(settings, db, server):
    """Ruta de producción completa: mensaje de Telegram -> trabajo -> el primario sin cuota -> responde
    el secundario -> el resultado indica qué motor respondió."""
    plan = json.dumps({"complejidad": "simple", "criterios_exito": ["saludar"], "pasos": []})
    groq = Engine(server, [_quota()])
    orouter = Engine(server, [_ok(plan), _ok("¡Hola! Te respondo desde el motor de respaldo.")])
    llm = LLM(db, [Provider("groq", groq.url, "gpt-oss", "k"), Provider("openrouter", orouter.url, "openrouter/free", "k")])
    m = FakeMessenger()
    app = App(settings, db=db, llm=llm, messenger=m)
    m.updates = [msg(text="hola", update_id=1)]
    rep = app.run_once(60)
    assert rep["done"] == 1
    out = m.texts()
    assert "motor de respaldo" in out and "⚙️ Motor: openrouter×2" in out
    job = db.one("SELECT counters FROM jobs")
    assert json.loads(job["counters"])["motores"] == {"openrouter:openrouter/free": 2}
