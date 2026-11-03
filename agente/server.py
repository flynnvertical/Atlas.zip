"""Modo servidor (webhook de Telegram) para hosts gratuitos sin GitHub Actions.

- POST /telegram  (cabecera X-Telegram-Bot-Api-Secret-Token obligatoria) -> encola el update.
- GET  /          -> chequeo de salud (sin datos).
- GET  /tick?k=…  -> despierta el ciclo (para un pinger externo gratuito que ejecute tareas programadas).
Un único hilo trabajador posee la base de datos; el estado se sincroniza cifrado con GitHub.
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .security import redact


class Worker(threading.Thread):
    def __init__(self, settings: Settings, messenger, sync=None, llm=None, slice_seconds: int = 90):
        super().__init__(daemon=True)
        self.s, self.messenger, self.sync, self.llm = settings, messenger, sync, llm
        self.updates: "queue.Queue[dict]" = queue.Queue()
        self.wake = threading.Event()
        self.slice = slice_seconds
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.last_error: str | None = None
        self.cycles = 0

    def run(self) -> None:
        from .app import App
        from .scheduler import run_due_schedules
        if self.sync:
            try:
                self.sync.pull()
            except Exception as e:  # noqa: BLE001
                self.last_error = redact(str(e))[:200]
                print("ERROR al restaurar estado:", self.last_error, flush=True)
                self.ready.set()
                return  # no arrancar con estado vacío: evitaría pisar el estado bueno
        app = App(self.s, llm=self.llm, messenger=self.messenger)
        self.app = app
        self.ready.set()
        while not self.stop.is_set():
            try:
                while True:
                    try:
                        app.handle_update(self.updates.get_nowait())
                    except queue.Empty:
                        break
                app.outbox.flush()
                run_due_schedules(app.db, self.s.timezone)
                app.process_jobs(time.time() + self.slice)
                app.outbox.flush()
                app.maintenance()
                if self.sync:
                    self.sync.push()
                self.cycles += 1
            except Exception as e:  # noqa: BLE001
                self.last_error = redact(f"{type(e).__name__}: {e}")[:200]
                print("ERROR en ciclo:", self.last_error, flush=True)
            pending = app.db.one("SELECT COUNT(*) c FROM jobs WHERE status='queued'")["c"]
            if not pending:
                self.wake.wait(timeout=60)
                self.wake.clear()


def make_handler(worker: Worker, secret: str, tick_key: str | None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # no registrar rutas ni contenido
            pass

        def _send(self, code: int, body: bytes = b"ok"):
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, b"agente ok" if worker.is_alive() else b"agente detenido")
            if u.path == "/tick":
                k = parse_qs(u.query).get("k", [""])[0]
                if tick_key and hmac.compare_digest(k, tick_key):
                    worker.wake.set()
                    return self._send(200)
                return self._send(403, b"no")
            return self._send(404, b"no")

        def do_POST(self):
            if urlparse(self.path).path != "/telegram":
                return self._send(404, b"no")
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not secret or not hmac.compare_digest(got, secret):
                return self._send(403, b"no")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                return self._send(413, b"no")
            try:
                upd = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, b"no")
            worker.updates.put(upd)
            worker.wake.set()
            return self._send(200)  # responder rápido: Telegram reintenta si tarda
    return H


def serve(settings: Settings, messenger, sync=None, port: int | None = None, llm=None,
          public_url: str | None = None) -> ThreadingHTTPServer:
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if len(secret) < 16:
        raise SystemExit("TELEGRAM_WEBHOOK_SECRET obligatorio (mínimo 16 caracteres, [A-Za-z0-9_-])")
    worker = Worker(settings, messenger, sync, llm)
    worker.start()
    worker.ready.wait(120)
    httpd = ThreadingHTTPServer(("0.0.0.0", port or int(os.environ.get("PORT", "8080"))),
                                make_handler(worker, secret, os.environ.get("AGENTE_TICK_KEY")))
    httpd.worker = worker
    if public_url and messenger:
        messenger.call("setWebhook", url=public_url.rstrip("/") + "/telegram", secret_token=secret,
                       allowed_updates=["message"], drop_pending_updates=False)
        print("Webhook configurado.", flush=True)
    return httpd
