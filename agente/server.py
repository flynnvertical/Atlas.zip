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
                if self.drain_inbox(app) and self.sync:
                    self.sync.push()  # persistir enseguida lo recibido, antes de trabajos largos
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
            paused = app.quota_paused_until() - time.time()
            if not pending or paused > 0:
                # Con cuota agotada no se reintenta en bucle: se despierta por mensajes nuevos
                # (comandos como /estado siguen funcionando) o al vencer la pausa.
                self.wake.wait(timeout=min(60, paused) if paused > 0 else 60)
                self.wake.clear()


    @staticmethod
    def drain_inbox(app) -> int:
        """Procesa el buzón en orden. Cada fila se borra recién después de manejarla; si el proceso
        se corta antes, se reprocesa y handle_update la deduplica por update_id."""
        n = 0
        for row in app.db.all("SELECT id, payload FROM inbox ORDER BY id LIMIT 100"):
            try:
                app.handle_update(json.loads(row["payload"]))
            except ValueError:
                app.db.log("warn", "inbox_invalido", str(row["id"]))
            app.db.execute("DELETE FROM inbox WHERE id=?", (row["id"],))
            n += 1
        return n


def store_update(db_path, payload: bytes) -> None:
    """Escritura durable e independiente del hilo trabajador (conexión propia, commit inmediato)."""
    import sqlite3
    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        conn.execute("INSERT INTO inbox(payload, received) VALUES(?, ?)", (payload.decode("utf-8"), time.time()))
        conn.commit()
    finally:
        conn.close()


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
            raw = self.rfile.read(n) or b"{}"
            try:
                json.loads(raw)
            except ValueError:
                return self._send(400, b"no")
            if not worker.is_alive() or not hasattr(worker, "app"):
                return self._send(503, b"no")  # Telegram reintentará más tarde
            try:
                store_update(worker.s.db_path, raw)  # durable ANTES de confirmar
            except Exception:  # noqa: BLE001
                return self._send(503, b"no")
            worker.wake.set()
            return self._send(200)
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
