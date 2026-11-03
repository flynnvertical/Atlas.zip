"""Orquestador: planificación, verificación, autocorrección, aprobaciones, recuperación y costos."""
import json
import time

from agente.agent import resolve_approval
from agente.llm import LLMError, QuotaExhausted, tool_call
from agente.memory import Memory
from agente.scheduler import claim_next, enqueue

from .conftest import AUDIT_OK, make_app, plan


def _run(app, objective="tarea", chat_id=111):
    jid = enqueue(app.db, chat_id, objective)
    job = claim_next(app.db)
    return jid, app.agent.run_job(job)


def test_simple_job_plans_then_answers(settings, db):
    app = make_app(settings, db, [plan("simple"), "¡Hola! ¿En qué te ayudo?"])
    jid, out = _run(app, "hola")
    assert out.status == "done" and "Hola" in out.text and "✅" in out.text
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert json.loads(j["plan"])["complejidad"] == "simple"
    # Las herramientas se envían siempre (gpt-oss las invoca aunque la tarea sea "simple")
    assert len(app.llm.calls) == 2 and app.llm.tools_sent[1] is True


def test_success_criteria_defined_before_execution(settings, db):
    app = make_app(settings, db, [plan("media", ["el resultado es 1210"]),
                                  tool_call("calculate", {"expression": "1000*1.21"}),
                                  "Total con IVA: 1210", AUDIT_OK])
    _, out = _run(app, "calculá 1000 + IVA 21%")
    assert out.status == "done"
    system = app.llm.calls[1][0]["content"]
    assert "el resultado es 1210" in system  # criterios en el contexto desde el inicio
    log = db.all("SELECT tool,status FROM action_log")
    assert [(r["tool"], r["status"]) for r in log] == [("calculate", "ok")]


def test_invented_url_triggers_self_correction(settings, db):
    app = make_app(settings, db, [
        plan(), "Según https://inventado.example/precios el precio es 50.",
        "No pude verificar el precio con fuentes consultadas [NO CONFIRMADO].", AUDIT_OK])
    _, out = _run(app)
    assert out.status == "done" and "✅" in out.text
    revision = app.llm.calls[2][-1]["content"]
    assert "VERIFICACIÓN AUTOMÁTICA" in revision and "inventado.example" in revision


def test_auditor_objection_revision_then_flagged(settings, db):
    obj = json.dumps({"cumple": False, "problemas": ["falta el informe solicitado"]})
    app = make_app(settings, db, [plan(), "respuesta 1", obj, "respuesta 2", obj])
    _, out = _run(app)
    assert out.status == "done"
    assert "NO superada" in out.text and "falta el informe" in out.text


def test_budget_exhaustion_returns_flagged_partial(settings, db):
    settings.budget.max_llm_calls = 4
    calc = tool_call("calculate", {"expression": "1+1"})
    app = make_app(settings, db, [plan(), calc, calc, "Parcial: sólo calculé 1+1."])
    _, out = _run(app)
    assert out.status == "done" and "PARCIAL" in out.text and "NO superada" in out.text
    assert len(app.llm.calls) == 4  # nunca supera el presupuesto


def test_bad_tool_calls_do_not_crash(settings, db):
    bad_json = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "calculate", "arguments": "{malformado"}}]}
    app = make_app(settings, db, [plan(), bad_json, tool_call("no_existe", {}, "b"),
                                  tool_call("calculate", {"expresion": "1"}, "c"), "listo", AUDIT_OK])
    _, out = _run(app)
    assert out.status == "done"
    tool_msgs = [m["content"] for m in app.llm.calls[-2] if m.get("role") == "tool"]
    assert any("JSON inválidos" in t for t in tool_msgs)
    assert any("inexistente" in t for t in tool_msgs)
    assert any("argumentos inválidos" in t for t in tool_msgs)


def test_transient_llm_error_retries_and_resumes_from_checkpoint(settings, db):
    app = make_app(settings, db, [plan(), tool_call("calculate", {"expression": "2*3"}),
                                  LLMError("HTTP 503"), "Resultado: 6", AUDIT_OK])
    jid, out = _run(app)
    assert out.status == "queued"
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert j["status"] == "queued" and "503" in j["error"]
    out2 = app.agent.run_job(claim_next(db))
    assert out2.status == "done" and "6" in out2.text
    # el plan y el resultado de la herramienta no se repitieron
    assert len(db.all("SELECT * FROM action_log WHERE tool='calculate'")) == 1


def test_failure_after_max_attempts(settings, db):
    settings.budget.max_attempts = 2
    app = make_app(settings, db, [plan(), LLMError("x"), LLMError("y")])
    jid, out = _run(app)
    assert out.status == "queued"
    out2 = app.agent.run_job(claim_next(db))
    assert out2.status == "failed" and "/reintentar" in out2.text


def test_quota_exhausted_requeues_without_consuming_attempt(settings, db):
    app = make_app(settings, db, [plan(), QuotaExhausted("429")])
    jid = enqueue(db, 111, "x")
    st = app.process_jobs(time.time() + 30)
    j = db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    assert st["quota"] and j["status"] == "queued" and j["attempts"] == 0
    assert "Cuota gratuita" in "\n".join(r["text"] for r in db.all("SELECT text FROM outbox"))


def test_side_effect_requires_approval_and_executes_once(settings, db, messenger):
    app = make_app(settings, db, [plan(), tool_call("delete_file", {"path": "viejo.txt"}),
                                  "Archivo eliminado.", AUDIT_OK], messenger)
    jid = enqueue(db, 111, "borrá viejo.txt")
    ws = settings.workspace_dir / f"trabajo_{jid}"
    ws.mkdir(parents=True)
    (ws / "viejo.txt").write_text("x")
    out = app.agent.run_job(claim_next(db))
    assert out.status == "waiting_approval" and (ws / "viejo.txt").exists()
    ap = db.one("SELECT * FROM approvals")
    assert ap["status"] == "pending" and "/aprobar" in messenger.texts()
    assert claim_next(db) is None  # no avanza sin decisión humana
    assert "aprobada" in resolve_approval(db, ap["id"], True, 111)
    out2 = app.agent.run_job(claim_next(db))
    assert out2.status == "done" and not (ws / "viejo.txt").exists()
    assert db.one("SELECT status FROM approvals")["status"] == "executed"
    assert "ya fue resuelta" in resolve_approval(db, ap["id"], True, 111)  # no se ejecuta dos veces


def test_rejected_action_not_executed(settings, db, messenger):
    app = make_app(settings, db, [plan(), tool_call("http_request", {"method": "POST", "url": "https://example.com/api"}),
                                  "No se envió porque lo rechazaste.", AUDIT_OK], messenger)
    jid = enqueue(db, 111, "publicá")
    assert app.agent.run_job(claim_next(db)).status == "waiting_approval"
    ap = db.one("SELECT * FROM approvals")
    assert "otro chat" in resolve_approval(db, ap["id"], False, 999)  # otro usuario no decide
    resolve_approval(db, ap["id"], False, 111)
    out = app.agent.run_job(claim_next(db))
    assert out.status == "done"
    assert not db.all("SELECT * FROM action_log WHERE tool='http_request'")
    assert any("RECHAZÓ" in (m.get("content") or "") for m in app.llm.calls[-2])


def test_get_requests_do_not_need_approval(ctx):
    from agente.tools import build_registry
    t = build_registry()["http_request"]
    assert t.needs_approval(ctx, {"method": "GET", "url": "https://x.org"}) is None
    assert t.needs_approval(ctx, {"method": "PUT", "url": "https://x.org"})
    assert build_registry()["memory_delete"].needs_approval(ctx, {"memory_id": 1})


def test_code_after_untrusted_content_needs_approval_without_isolation(ctx, monkeypatch):
    from agente.tools import build_registry
    from agente.tools import code as code_t
    t = build_registry()["run_python"]
    monkeypatch.setattr(code_t, "fs_isolation_available", lambda: True)
    monkeypatch.setattr(code_t, "network_isolation_available", lambda: False)
    assert t.needs_approval(ctx, {"code": "print(1)"}) is None
    ctx.tainted = True
    assert "exfiltración" in t.needs_approval(ctx, {"code": "print(1)"})
    monkeypatch.setattr(code_t, "network_isolation_available", lambda: True)
    assert t.needs_approval(ctx, {"code": "print(1)"}) is None


def test_interrupted_job_recovered_after_lease_expiry(settings, db):
    app = make_app(settings, db, [plan(), tool_call("calculate", {"expression": "7*6"})])
    jid = enqueue(db, 111, "x")
    job = claim_next(db)
    try:  # el guion se agota tras la herramienta: simula una caída a mitad del trabajo
        app.agent.run_job(job)
    except LLMError:
        pass
    db.execute("UPDATE jobs SET status='running', lease_until=? WHERE id=?", (time.time() - 1, jid))
    app2 = make_app(settings, db, ["El resultado es 42", AUDIT_OK])
    job2 = claim_next(db)
    assert job2["id"] == jid and job2["attempts"] == 2
    out = app2.agent.run_job(job2)
    assert out.status == "done" and "42" in out.text
    assert any(m.get("role") == "tool" and "42" in m["content"] for m in app2.llm.calls[0])
    assert db.one("SELECT COUNT(*) c FROM events WHERE kind='job_recovered'")["c"] == 1


def test_memory_context_injected_and_learned(settings, db):
    Memory(db).save("Siempre entregá montos en pesos argentinos", kind="instruction", source="user")
    app = make_app(settings, db, [plan(), tool_call("memory_save", {"content": "Cliente Z pidió 40 cables", "kind": "project"}),
                                  "Anotado.", AUDIT_OK])
    _run(app, "registrá que el cliente Z pidió 40 cables")
    assert "pesos argentinos" in app.llm.calls[1][0]["content"]
    assert Memory(db).search("cliente cables")[0]["source"] == "agent"


def test_generated_files_are_delivered(settings, db, messenger):
    app = make_app(settings, db, [plan(), tool_call("write_file", {"path": "informe.md", "content": "# Hola"}),
                                  "Generé informe.md", AUDIT_OK], messenger)
    enqueue(db, 111, "hacé un informe")
    app.process_jobs(time.time() + 30)
    assert messenger.docs and messenger.docs[0][1].endswith("informe.md")
    assert "Generé informe.md" in messenger.texts()


def test_context_compaction_keeps_requests_small(settings, db):
    app = make_app(settings, db, [])
    settings.context_chars = 5000
    msgs = [{"role": "system", "content": "s"}] + [{"role": "tool", "tool_call_id": str(i), "content": "x" * 4000}
                                                   for i in range(6)]
    out = app.agent._compact(msgs)
    assert sum(len(m["content"]) for m in out) < 12000
    assert out[-1]["content"] == "x" * 4000 or len(out[-1]["content"]) <= 3000



def test_false_memory_claim_is_caught_and_corrected(settings, db):
    """Caso real del CI: el modelo dijo 'lo guardé en memoria' sin llamar a memory_save."""
    app = make_app(settings, db, [
        plan("simple"), "Listo, guardé en tu memoria que tu empresa es Electro Sur.",
        tool_call("memory_save", {"content": "La empresa del usuario es Electro Sur", "kind": "fact"}),
        "Guardado en memoria: tu empresa es Electro Sur."])
    _, out = _run(app, "guardá en tu memoria que mi empresa es Electro Sur")
    assert out.status == "done" and "✅" in out.text
    revision = app.llm.calls[2][-1]["content"]
    assert "memory_save" in revision
    assert Memory(db).search("empresa electro sur")


def test_empty_model_answer_is_requested_again(settings, db):
    """Caso real (L06): gpt-oss devolvió contenido vacío como respuesta final."""
    app = make_app(settings, db, [plan(), {"role": "assistant", "content": ""}, "Total general: 25000", AUDIT_OK])
    _, out = _run(app)
    assert out.status == "done" and "25000" in out.text and "✅" in out.text
    assert "llegó vacía" in app.llm.calls[2][-1]["content"]


def test_memory_context_includes_recent_facts_without_word_match(db):
    """Caso real (L14): se guardó 'venta de materiales' y se preguntó '¿qué vende mi empresa?'."""
    Memory(db).save('{"nombre": "Electro Sur SRL", "actividad": "venta de materiales eléctricos"}',
                    kind="fact", source="agent")
    assert "Electro Sur SRL" in Memory(db).context_for("¿Cómo se llama mi empresa y qué vende?")
