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
