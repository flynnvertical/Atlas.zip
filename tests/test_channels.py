"""Telegram, comandos, programación, cola de salida y proveedores LLM (HTTP real contra servidores locales)."""
import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

import pytest

from agente.app import App
from agente.llm import LLM, LLMError, Provider, QuotaExhausted, ScriptedLLM, tool_call
from agente.scheduler import add_schedule, next_run_after, run_due_schedules
from agente.telegram import Telegram

from .conftest import AUDIT_OK, FakeMessenger, make_app, msg, plan


# --- autorización y deduplicación -----------------------------------------------
def test_unauthorized_users_ignored_but_id_answered(settings, db, messenger):
    app = make_app(settings, db, [], messenger)
    app.handle_update(msg(user_id=999, chat_id=999, text="borrá todo", update_id=1))
    app.handle_update(msg(user_id=999, chat_id=999, text="/id", update_id=2))
    app.outbox.flush()
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 0
    assert messenger.sent == [(999, "Tu ID de Telegram es 999. Agregalo al secreto TELEGRAM_ALLOWED_USERS para autorizarte.")]


def test_duplicate_updates_processed_once(settings, db, messenger):
    app = make_app(settings, db, [], messenger)
    for _ in range(3):
        app.handle_update(msg(text="investigá algo", update_id=42))
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1
    assert db.kv_get("tg_offset") == "43"


def test_commands_memory_and_status(settings, db, messenger):
    app = make_app(settings, db, [], messenger)
    assert "mem#1" in app.command(111, "/recordar Siempre respondé con tablas")
    assert "Siempre respondé" in app.command(111, "/memoria tablas")
    assert "Eliminada" in app.command(111, "/olvidar 1")
    assert "Sin trabajos activos" in app.command(111, "/estado")
    assert "comandos" in app.command(111, "/ayuda")
    assert "no reconocido" in app.command(111, "/inventado")


def test_cancel_and_retry_commands(settings, db, messenger):
    app = make_app(settings, db, [], messenger)
    app.handle_update(msg(text="tarea larga", update_id=5))
    assert "cancelado" in app.command(111, "/cancelar 1")
    assert db.one("SELECT status FROM jobs WHERE id=1")["status"] == "cancelled"
    assert "#2" in app.command(111, "/reintentar 1")
    assert "No existe" in app.command(222, "/ver 1")  # aislamiento entre chats


# --- programación de tareas -----------------------------------------------------
def test_schedule_minimum_interval_and_limits(settings, db):
    with pytest.raises(ValueError, match="mínimo"):
        add_schedule(db, 111, "x", "every:5", settings.timezone)
    with pytest.raises(ValueError):
        add_schedule(db, 111, "x", "daily:25:00", settings.timezone)
    for i in range(10):
        add_schedule(db, 111, f"t{i}", "every:60", settings.timezone)
    with pytest.raises(ValueError, match="máximo"):
        add_schedule(db, 111, "x", "every:60", settings.timezone)


def test_daily_schedule_respects_timezone():
    tz = "America/Argentina/Buenos_Aires"
    base = dt.datetime(2026, 11, 3, 10, 0, tzinfo=ZoneInfo(tz)).timestamp()
    nxt = dt.datetime.fromtimestamp(next_run_after("daily:08:30", tz, base), ZoneInfo(tz))
    assert (nxt.day, nxt.hour, nxt.minute) == (4, 8, 30)


def test_due_schedule_enqueued_once(settings, db):
    sid = add_schedule(db, 111, "resumen diario", "every:60", settings.timezone)
    due = db.one("SELECT next_run FROM schedules WHERE id=?", (sid,))["next_run"]
    db.execute("UPDATE schedules SET next_run=? WHERE id=?", (due - 4000, sid))
    snapshot = db.one("SELECT next_run FROM schedules")["next_run"]
    assert len(run_due_schedules(db, settings.timezone)) == 1
    db.execute("UPDATE schedules SET next_run=?", (snapshot,))  # simula corrida repetida
    assert run_due_schedules(db, settings.timezone) == []
    assert db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1


# --- cola de salida ---------------------------------------------------------------
def test_outbox_retries_without_losing_messages(settings, db):
    m = FakeMessenger(fail_times=2)
    app = make_app(settings, db, [], m)
    app.outbox.put(111, "resultado importante")
    assert app.outbox.flush() == (0, 1)
    assert app.outbox.flush() == (0, 1)
    assert app.outbox.flush() == (1, 0)
    assert m.sent == [(111, "resultado importante")]


def test_outbox_redacts_secrets(settings, db, messenger):
    app = make_app(settings, db, [], messenger)
    app.outbox.put(111, "token 123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
    app.outbox.flush()
    assert "ABCDEFGHIJ" not in messenger.texts()


# --- Telegram real contra servidor Bot API local --------------------------------
class FakeBotAPI:
    def __init__(self, server, updates):
        self.updates = updates
        self.sent, self.docs = [], []
        self.srv = server({"/botTEST:TOKEN/*": self.route, "/file/botTEST:TOKEN/*": self.file})

    def route(self, method, path, body):
        name = path.rsplit("/", 1)[-1]
        if name == "getUpdates":
            p = json.loads(body or b"{}")
            res = [u for u in self.updates if u["update_id"] >= p.get("offset", 0)]
            return 200, {"Content-Type": "application/json"}, json.dumps({"ok": True, "result": res}).encode()
        if name == "sendMessage":
            self.sent.append(json.loads(body))
        elif name == "sendDocument":
            self.docs.append(body)
        elif name == "getFile":
            return 200, {}, json.dumps({"ok": True, "result": {"file_path": "docs/a.csv", "file_size": 10}}).encode()
        return 200, {"Content-Type": "application/json"}, json.dumps({"ok": True, "result": True}).encode()

    def file(self, method, path, body):
        return 200, {}, b"producto,precio\ncable,100\n"


def test_telegram_client_chunks_long_messages(server):
    api = FakeBotAPI(server, [])
    tg = Telegram("TEST:TOKEN", api_base=api.srv.url)
    tg.send_message(5, "línea\n" * 1500)
    assert len(api.sent) == 3 and all(len(m["text"]) <= 4000 for m in api.sent)


def test_end_to_end_telegram_run_once(settings, db, server):
    """Mensaje entrante -> trabajo -> herramienta -> documento -> respuesta y archivo por Telegram."""
    api = FakeBotAPI(server, [msg(text="Generá un CSV con 2 productos", update_id=10)])
    tg = Telegram("TEST:TOKEN", api_base=api.srv.url)
    script = [plan(), tool_call("write_file", {"path": "productos.csv", "content": "p,precio\na,1\nb,2"}),
              "Listo: productos.csv con 2 productos.", AUDIT_OK]
    app = App(settings, db=db, llm=ScriptedLLM(script), messenger=tg)
    rep = app.run_once(60)
    assert rep["updates"] == 1 and rep["done"] == 1
    texts = [m["text"] for m in api.sent]
    assert any("Trabajo #1 en cola" in t for t in texts) and any("productos.csv con 2" in t for t in texts)
    assert len(api.docs) == 1 and b"productos.csv" in api.docs[0]
    rep2 = app.run_once(60)  # siguiente corrida: no reprocesa el mismo mensaje
    assert rep2["done"] == 0 and db.one("SELECT COUNT(*) c FROM jobs")["c"] == 1


def test_document_upload_creates_analysis_job(settings, db, server):
    up = {"update_id": 20, "message": {"chat": {"id": 111}, "from": {"id": 111}, "caption": "Analizá precios",
                                       "document": {"file_id": "F1", "file_name": "lista.csv"}}}
    api = FakeBotAPI(server, [up])
    app = App(settings, db=db, llm=ScriptedLLM([]), messenger=Telegram("TEST:TOKEN", api_base=api.srv.url))
    app.handle_update(up)
    job = db.one("SELECT objective FROM jobs")
    assert "Analizá precios" in job["objective"] and "read_file" in job["objective"]
    saved = list((settings.workspace_dir / "compartido").glob("*lista.csv"))
    assert saved and "cable,100" in saved[0].read_text()


# --- proveedores LLM con respaldo y control de costos ----------------------------
def _provider_server(server, status_seq):
    seq = list(status_seq)

    def route(method, path, body):
        st = seq.pop(0) if seq else 200
        if st != 200:
            return st, {"Content-Type": "application/json"}, b'{"error":"rate limit"}'
        return 200, {"Content-Type": "application/json"}, json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}).encode()
    return server({"/v1/chat/completions": route})


def test_llm_fallback_on_quota_and_usage_accounting(server, db):
    a, b = _provider_server(server, [429]), _provider_server(server, [])
    llm = LLM(db, [Provider("a", a.url + "/v1/chat/completions", "m", "k1"),
                   Provider("b", b.url + "/v1/chat/completions", "m", "k2")], sleep=lambda s: None)
    assert llm.chat([{"role": "user", "content": "hola"}])["content"] == "ok"
    assert llm.last_provider == "b:m"
    rows = {r["provider"]: dict(r) for r in db.all("SELECT * FROM usage")}
    assert rows["a"]["errors"] == 1 and rows["b"]["calls"] == 1 and rows["b"]["prompt_tokens"] == 10


def test_llm_retries_server_errors(server, db):
    s = _provider_server(server, [503])
    llm = LLM(db, [Provider("a", s.url + "/v1/chat/completions", "m", None)], sleep=lambda x: None)
    assert llm.chat([{"role": "user", "content": "x"}])["content"] == "ok"


def test_llm_all_quota_raises_quota_exhausted(server, db):
    s = _provider_server(server, [429, 429])
    llm = LLM(db, [Provider("a", s.url + "/v1/chat/completions", "m", None)], sleep=lambda x: None)
    with pytest.raises(QuotaExhausted):
        llm.chat([{"role": "user", "content": "x"}])


def test_daily_cap_blocks_before_network(db):
    llm = LLM(db, [Provider("a", "http://127.0.0.1:9/never", "m", None)], daily_cap=1)
    db.execute("INSERT INTO usage(day,provider,calls) VALUES(?,?,?)", (llm._today(), "a", 1))
    with pytest.raises(QuotaExhausted, match="Tope diario"):
        llm.chat([{"role": "user", "content": "x"}])


def test_no_provider_configured_is_explicit(db):
    with pytest.raises(LLMError, match="No hay proveedor"):
        LLM(db, []).chat([])


def test_llm_errors_never_leak_keys(server, db):
    def route(method, path, body):
        return 401, {}, b'{"error":"bad key sk-abcdefghijklmnopqrstuvwxyz123"}'
    s = server({"/v1/chat/completions": route})
    llm = LLM(db, [Provider("a", s.url + "/v1/chat/completions", "m", "sk-abcdefghijklmnopqrstuvwxyz123")])
    with pytest.raises(LLMError) as e:
        llm.chat([{"role": "user", "content": "x"}])
    assert "abcdefghijklmnop" not in str(e.value)
