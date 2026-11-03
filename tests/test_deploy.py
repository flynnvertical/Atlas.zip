"""Despliegue sin Actions: sincronización de estado vía API de GitHub y servidor webhook."""
import base64
import hashlib
import json
import threading
import time

import pytest
import requests

from agente.db import DB
from agente.llm import ScriptedLLM, tool_call
from agente.memory import Memory
from agente.state_sync import GitHubStateSync, SyncError
from agente.telegram import Telegram

from .conftest import AUDIT_OK, msg, plan
from .test_channels import FakeBotAPI

KEY = "clave-sync-de-prueba-123456"


class FakeGitHub:
    """Imita los endpoints de la API de contenidos que usa GitHubStateSync."""

    def __init__(self, server):
        self.files: dict[str, tuple[bytes, str]] = {}
        self.branches = {"main"}
        self.puts = 0
        self.srv = server({"/repos/o/r*": self.route})

    def route(self, method, path, body):
        js = {"Content-Type": "application/json"}
        p = path.split("?")[0]
        if p == "/repos/o/r":
            return 200, js, json.dumps({"default_branch": "main"}).encode()
        if p.startswith("/repos/o/r/git/ref/heads/"):
            b = p.rsplit("/", 1)[-1]
            return (200 if b in self.branches else 404), js, json.dumps({"object": {"sha": "abc"}}).encode()
        if p == "/repos/o/r/git/refs" and method == "POST":
            self.branches.add(json.loads(body)["ref"].split("/")[-1])
            return 201, js, b"{}"
        if p == "/repos/o/r/contents/estado.enc":
            if method == "GET":
                if "estado.enc" not in self.files:
                    return 404, js, b"{}"
                return 200, js, json.dumps({"sha": self.files["estado.enc"][1]}).encode()
            if method == "PUT":
                b = json.loads(body)
                cur = self.files.get("estado.enc")
                if cur and b.get("sha") != cur[1]:
                    return 409, js, b'{"message":"sha mismatch"}'
                data = base64.b64decode(b["content"])
                sha = hashlib.sha1(data).hexdigest()
                self.files["estado.enc"] = (data, sha)
                self.puts += 1
                return 201, js, json.dumps({"content": {"sha": sha}}).encode()
        return 404, js, b"{}"


def _raw_get_patch(monkeypatch, gh):
    """El servidor simulado no distingue el Accept raw: devolvemos el binario para esa variante."""
    real_get = requests.get

    def get(url, headers=None, **kw):
        if headers and "raw" in headers.get("Accept", "") and url.endswith("/contents/estado.enc"):
            r = requests.models.Response()
            r.status_code, r._content = 200, gh.files["estado.enc"][0]
            return r
        return real_get(url, headers=headers, **kw)
    monkeypatch.setattr("agente.state_sync.requests.get", get)


def test_state_sync_roundtrip_between_two_hosts(tmp_path, server, monkeypatch):
    gh = FakeGitHub(server)
    _raw_get_patch(monkeypatch, gh)
    host1 = tmp_path / "h1" / "data"
    s1 = GitHubStateSync("o/r", "tok", KEY, host1, api=gh.srv.url)
    assert s1.pull() is False  # primera vez: no hay estado
    d = DB(host1 / "agente.db")
    Memory(d).save("cliente preferido: Ferretería Andes", source="user")
    d.close()
    assert s1.push() is True and "agent-state" in gh.branches
    assert s1.push() is False  # sin cambios: no sube de nuevo
    assert b"Andes" not in gh.files["estado.enc"][0]  # en GitHub sólo hay datos cifrados
    host2 = tmp_path / "h2" / "data"
    s2 = GitHubStateSync("o/r", "tok", KEY, host2, api=gh.srv.url)
    assert s2.pull() is True
    assert Memory(DB(host2 / "agente.db")).search("ferreteria andes")


def test_state_sync_detects_concurrent_writer(tmp_path, server, monkeypatch):
    gh = FakeGitHub(server)
    _raw_get_patch(monkeypatch, gh)
    a = GitHubStateSync("o/r", "tok", KEY, tmp_path / "a", api=gh.srv.url)
    b = GitHubStateSync("o/r", "tok", KEY, tmp_path / "b", api=gh.srv.url)
    for s, txt in ((a, "A"), (b, "B")):
        s.pull()
        dd = DB(s.data_dir / "agente.db")
        Memory(dd).save(f"dato {txt}")
        dd.close()
    a.push()
    with pytest.raises(SyncError, match="otra instancia"):
        b.push()  # no pisa silenciosamente el estado de otra instancia


def test_webhook_server_end_to_end(settings, server, monkeypatch):
    from agente.server import serve
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secreto-webhook-1234567")
    api = FakeBotAPI(server, [])
    tg = Telegram("TEST:TOKEN", api_base=api.srv.url)
    llm = ScriptedLLM([plan(), tool_call("calculate", {"expression": "2**10"}), "2 a la 10 es 1024", AUDIT_OK])
    httpd = serve(settings, tg, sync=None, port=0, llm=llm)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        assert requests.get(base + "/").text == "agente ok"
        body = json.dumps(msg(text="cuánto es 2^10", update_id=77))
        assert requests.post(base + "/telegram", data=body).status_code == 403  # sin secreto
        assert requests.post(base + "/telegram", data=body,
                             headers={"X-Telegram-Bot-Api-Secret-Token": "otro-secreto-xxxxxxxx"}).status_code == 403
        r = requests.post(base + "/telegram", data=body,
                          headers={"X-Telegram-Bot-Api-Secret-Token": "secreto-webhook-1234567"})
        assert r.status_code == 200
        for _ in range(100):
            if any("1024" in m["text"] for m in api.sent):
                break
            time.sleep(0.1)
        texts = [m["text"] for m in api.sent]
        assert any("en cola" in t for t in texts) and any("1024" in t for t in texts)
    finally:
        httpd.worker.stop.set()
        httpd.worker.wake.set()
        httpd.shutdown()


def test_webhook_requires_strong_secret(settings, monkeypatch):
    from agente.server import serve
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "corto")
    with pytest.raises(SystemExit):
        serve(settings, None, port=0)


# --- hallazgo P1 (Codex): espera ante cuota agotada ---------------------------------
def test_quota_pauses_processing_without_burning_budget(settings, db):
    from agente.app import App
    from agente.llm import QuotaExhausted
    from agente.scheduler import enqueue
    llm = ScriptedLLM([QuotaExhausted("Todos los proveedores devolvieron 429")])
    app = App(settings, db=db, llm=llm, messenger=None)
    jid = enqueue(db, 111, "tarea")
    st = app.process_jobs(time.time() + 30)
    assert st["quota"] and 250 < st["paused_until"] - time.time() <= 300
    # durante la pausa no se vuelve a llamar al modelo (el guion vacío fallaría si se llamara)
    st2 = app.process_jobs(time.time() + 30)
    assert st2["quota"] and len(llm.calls) == 1
    job = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert job["status"] == "queued" and json.loads(job["counters"] or "{}").get("llm_calls", 0) == 0


def test_daily_cap_pauses_until_next_utc_day(settings, db):
    from agente.app import App
    from agente.llm import QuotaExhausted
    from agente.scheduler import enqueue
    app = App(settings, db=db, llm=ScriptedLLM([QuotaExhausted("Tope diario propio alcanzado (120 llamadas).")]))
    enqueue(db, 111, "tarea")
    until = app.process_jobs(time.time() + 30)["paused_until"]
    assert until % 86400 == 60 and until > time.time()


def test_server_worker_does_not_spin_on_quota(settings, server, monkeypatch):
    from agente.llm import QuotaExhausted
    from agente.server import serve
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secreto-webhook-1234567")
    api = FakeBotAPI(server, [])
    llm = ScriptedLLM([QuotaExhausted("429")] * 50)
    httpd = serve(settings, Telegram("TEST:TOKEN", api_base=api.srv.url), sync=None, port=0, llm=llm)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        r = requests.post(base + "/telegram", data=json.dumps(msg(text="tarea", update_id=5)),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "secreto-webhook-1234567"})
        assert r.status_code == 200
        time.sleep(2.5)
        assert len(llm.calls) == 1  # antes de la corrección: decenas de reintentos por segundo
        # los comandos siguen respondiendo durante la pausa
        requests.post(base + "/telegram", data=json.dumps(msg(text="/estado", update_id=6)),
                      headers={"X-Telegram-Bot-Api-Secret-Token": "secreto-webhook-1234567"})
        for _ in range(50):
            if any("Estado" in m["text"] for m in api.sent):
                break
            time.sleep(0.1)
        assert any("Estado" in m["text"] for m in api.sent)
    finally:
        httpd.worker.stop.set()
        httpd.worker.wake.set()
        httpd.shutdown()


# --- hallazgo P2 (Codex): buzón durable del webhook ---------------------------------
def test_webhook_update_survives_restart_before_processing(settings, server, monkeypatch):
    from agente.app import App
    from agente.server import Worker, serve
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "secreto-webhook-1234567")
    monkeypatch.setattr(Worker, "drain_inbox", staticmethod(lambda app: 0))  # "cae" antes de procesar
    api = FakeBotAPI(server, [])
    httpd = serve(settings, Telegram("TEST:TOKEN", api_base=api.srv.url), sync=None, port=0, llm=ScriptedLLM([]))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        r = requests.post(base + "/telegram", data=json.dumps(msg(text="investigá algo", update_id=9)),
                          headers={"X-Telegram-Bot-Api-Secret-Token": "secreto-webhook-1234567"})
        assert r.status_code == 200
    finally:
        httpd.worker.stop.set()
        httpd.worker.wake.set()
        httpd.shutdown()
    monkeypatch.undo()
    # "reinicio": proceso nuevo sobre la misma base -> el mensaje confirmado no se perdió
    d2 = DB(settings.db_path)
    app2 = App(settings, db=d2, llm=ScriptedLLM([]), messenger=None)
    assert Worker.drain_inbox(app2) == 1
    assert d2.one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    d2.execute("INSERT INTO inbox(payload, received) VALUES(?, ?)",
               (json.dumps(msg(text="investigá algo", update_id=9)), time.time()))  # reentrega
    Worker.drain_inbox(app2)
    assert d2.one("SELECT COUNT(*) c FROM jobs")["c"] == 1  # sin duplicados


def test_webhook_returns_503_when_worker_is_down(settings, monkeypatch):
    from agente.server import make_handler
    from http.server import ThreadingHTTPServer

    class Dead:
        s = settings

        def is_alive(self):
            return False
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Dead(), "secreto-webhook-1234567", None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        r = requests.post(f"http://127.0.0.1:{httpd.server_address[1]}/telegram", data="{}",
                          headers={"X-Telegram-Bot-Api-Secret-Token": "secreto-webhook-1234567"})
        assert r.status_code == 503  # Telegram reintenta en lugar de darlo por entregado
    finally:
        httpd.shutdown()
