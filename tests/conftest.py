import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agente.app import App  # noqa: E402
from agente.config import Settings  # noqa: E402
from agente.db import DB  # noqa: E402
from agente.llm import ScriptedLLM  # noqa: E402
from agente.memory import Memory  # noqa: E402
from agente.tools import ToolContext  # noqa: E402


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AGENTE_DATA_DIR", str(tmp_path / "data"))
    s = Settings.from_env()
    s.telegram_allowed = {111}
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings):
    d = DB(settings.db_path)
    yield d
    d.close()


@pytest.fixture
def ctx(settings, db):
    ws = settings.workspace_dir / "trabajo_1"
    ws.mkdir(parents=True, exist_ok=True)
    return ToolContext(settings=settings, db=db, memory=Memory(db), job_id=1, chat_id=111, workspace=ws)


class FakeMessenger:
    def __init__(self, fail_times: int = 0):
        self.sent: list[tuple[int, str]] = []
        self.docs: list[tuple[int, str]] = []
        self.fail_times = fail_times
        self.updates: list[dict] = []

    def send_message(self, chat_id, text):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("red caída")
        self.sent.append((chat_id, text))

    def send_document(self, chat_id, path, caption=""):
        self.docs.append((chat_id, path))

    def get_updates(self, offset, timeout=0):
        ups = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        return ups

    def texts(self):
        return "\n".join(t for _, t in self.sent)


@pytest.fixture
def messenger():
    return FakeMessenger()


def make_app(settings, db, script, messenger=None):
    return App(settings, db=db, llm=ScriptedLLM(script), messenger=messenger)


def plan(complexity="media", criteria=None):
    return json.dumps({"complejidad": complexity, "criterios_exito": criteria or ["responder correctamente"],
                       "pasos": ["resolver"]})


AUDIT_OK = json.dumps({"cumple": True, "problemas": []})


def msg(chat_id=111, user_id=111, text="hola", update_id=1):
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "from": {"id": user_id}, "text": text}}


class LocalServer:
    """Servidor HTTP local real para probar código de red sin depender de internet."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                outer.requests.append((self.command, self.path, body))
                fn = outer.routes.get(self.path.split("?")[0])
                if fn is None:
                    for k, v in outer.routes.items():
                        if k.endswith("*") and self.path.startswith(k[:-1]):
                            fn = v
                if fn is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                status, headers, data = fn(self.command, self.path, body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = _handle

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def server():
    servers = []

    def factory(routes):
        s = LocalServer(routes)
        servers.append(s)
        return s
    yield factory
    for s in servers:
        s.close()
