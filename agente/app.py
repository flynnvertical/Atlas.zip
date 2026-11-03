"""Ciclo de ejecución: recibe mensajes, gestiona comandos, procesa la cola y entrega resultados."""
from __future__ import annotations

import hmac
import json
import os
import re
import time
from pathlib import Path

from .agent import Agent, resolve_approval
from .config import Settings
from .db import DB
from .llm import LLM, QuotaExhausted
from .memory import Memory
from .scheduler import add_schedule, claim_next, enqueue, run_due_schedules
from .security import redact

MAX_PAIR_FAILS = 5

HELP = """🤖 AGENTE SUPREMO — comandos
Escribí cualquier objetivo en lenguaje natural y lo ejecuto (investigar, calcular, generar
documentos, analizar archivos que me envíes...).

/estado – trabajos activos, aprobaciones y consumo
/tareas – últimos trabajos · /ver N – resultado del trabajo N
/cancelar N · /reintentar N
/aprobar N · /rechazar N – acciones que requieren tu permiso
/recordar <texto> – guardar preferencia o instrucción
/memoria [búsqueda] – ver memorias · /olvidar N · /olvidar todo
/programar every:120 <objetivo> | /programar daily:08:30 <objetivo>
/programas · /desprogramar N
/uso – consumo de cuota de modelos
/id – tu identificador de Telegram"""


class Outbox:
    """Cola de salida persistente: los resultados no se pierden si falla el envío."""

    def __init__(self, db: DB, messenger=None):
        self.db = db
        self.messenger = messenger

    def put(self, chat_id: int, text: str | None = None, file_path: str | None = None) -> None:
        self.db.execute("INSERT INTO outbox(chat_id,text,file_path,created) VALUES(?,?,?,?)",
                        (chat_id, redact(text) if text else None, file_path, time.time()))

    def flush(self) -> tuple[int, int]:
        sent = failed = 0
        if not self.messenger:
            return 0, 0
        for r in self.db.all("SELECT * FROM outbox WHERE status='pending' ORDER BY id LIMIT 50"):
            try:
                if r["file_path"]:
                    if Path(r["file_path"]).exists():
                        self.messenger.send_document(r["chat_id"], r["file_path"], r["text"] or "")
                else:
                    self.messenger.send_message(r["chat_id"], r["text"] or "")
                self.db.execute("UPDATE outbox SET status='sent', attempts=attempts+1 WHERE id=?", (r["id"],))
                sent += 1
            except Exception as e:  # noqa: BLE001
                st = "failed" if r["attempts"] + 1 >= 5 else "pending"
                self.db.execute("UPDATE outbox SET status=?, attempts=attempts+1 WHERE id=?", (st, r["id"]))
                self.db.log("warn", "send_failed", redact(str(e))[:300])
                failed += 1
        return sent, failed


class App:
    def __init__(self, settings: Settings, db: DB | None = None, llm=None, messenger=None):
        self.s = settings
        self.s.ensure_dirs()
        self.db = db or DB(settings.db_path)
        self.llm = llm or LLM(self.db, daily_cap=settings.llm_daily_cap)
        self.messenger = messenger
        self.outbox = Outbox(self.db, messenger)
        self.memory = Memory(self.db)
        self.agent = Agent(settings, self.db, self.llm, notify=self._notify)

    def _notify(self, chat_id: int, text: str) -> None:
        self.outbox.put(chat_id, text)
        self.outbox.flush()  # avisos (aprobaciones, progreso) salen de inmediato

    # ------------------------------------------------------------------ entrada
    def owners(self) -> set[int]:
        try:
            return {int(x) for x in json.loads(self.db.kv_get("owner_ids", "[]") or "[]")}
        except ValueError:
            return set()

    def authorized(self, user_id: int) -> bool:
        return user_id in self.s.telegram_allowed or user_id in self.owners()

    def _pair(self, chat_id: int, user_id: int, chat_type: str, text: str) -> str | None:
        """Vinculación del dueño con código (AGENTE_PAIRING_CODE). Devuelve la respuesta o None."""
        code = os.environ.get("AGENTE_PAIRING_CODE", "")
        if self.authorized(user_id):
            return "Ya estás autorizado."
        if len(code) < 8:
            return None  # vinculación deshabilitada: no revelar nada
        if self.owners() or self.s.telegram_allowed:
            return None  # ya hay dueño: la vinculación queda cerrada para siempre
        if chat_type != "private":
            return "La vinculación sólo funciona en un chat privado con el bot."
        fails = int(self.db.kv_get("pair_fail", "0") or 0)
        if fails >= MAX_PAIR_FAILS:
            return None  # bloqueada tras intentos fallidos
        given = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
        if not hmac.compare_digest(given.encode(), code.encode()):
            self.db.kv_set("pair_fail", str(fails + 1))
            self.db.log("warn", "pair_failed", {"intentos": fails + 1})
            return "Código incorrecto." if fails + 1 < MAX_PAIR_FAILS else None
        self.db.kv_set("owner_ids", json.dumps([user_id]))
        self.db.log("info", "paired", {"user": "redactado"})
        return (f"✅ Vinculado: tu ID {user_id} quedó registrado como dueño (guardado en el estado cifrado). "
                "La vinculación quedó cerrada. Ya podés borrar el secreto AGENTE_PAIRING_CODE.\n\n" + HELP)

    def handle_update(self, upd: dict) -> None:
        uid = upd.get("update_id")
        if uid is not None:
            if self.db.one("SELECT 1 FROM processed_updates WHERE update_id=?", (uid,)):
                return  # deduplicación: nunca procesar dos veces el mismo mensaje
            self.db.execute("INSERT INTO processed_updates(update_id,ts) VALUES(?,?)", (uid, time.time()))
            last = int(self.db.kv_get("tg_offset", "0") or 0)
            if uid + 1 > last:
                self.db.kv_set("tg_offset", str(uid + 1))
        msg = upd.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        user_id = (msg.get("from") or {}).get("id")
        if chat_id is None or user_id is None:
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if text.split("@")[0].split(" ")[0].lower() == "/vincular":
            reply = self._pair(chat_id, user_id, (msg.get("chat") or {}).get("type", ""), text)
            if reply:
                self.outbox.put(chat_id, reply)
            return
        if text.startswith("/id"):
            self.outbox.put(chat_id, f"Tu ID de Telegram es {user_id}. Agregalo al secreto "
                                     "TELEGRAM_ALLOWED_USERS para autorizarte.")
            return
        if not self.authorized(user_id):
            self.db.log("warn", "unauthorized", {"user": "redactado"})
            return
        if msg.get("document"):
            self._receive_document(chat_id, msg, text)
            return
        if not text:
            self.outbox.put(chat_id, "Por ahora proceso texto y documentos (PDF, DOCX, XLSX, CSV, JSON, TXT).")
            return
        if text.startswith("/"):
            self.outbox.put(chat_id, self.command(chat_id, text))
            return
        jid = enqueue(self.db, chat_id, text, origin="telegram", dedupe_key=f"tg:{uid}" if uid is not None else None)
        if jid:
            self.outbox.put(chat_id, f"📥 Recibido. Trabajo #{jid} en cola.")

    def _receive_document(self, chat_id: int, msg: dict, caption: str) -> None:
        doc = msg["document"]
        name = re.sub(r"[^\w.\- ]", "_", doc.get("file_name") or "archivo")[:80]
        dest = self.s.workspace_dir / "compartido" / f"{int(time.time())}_{name}"
        try:
            if not self.messenger or not hasattr(self.messenger, "download_file"):
                raise RuntimeError("canal sin descarga")
            self.messenger.download_file(doc["file_id"], dest)
        except Exception as e:  # noqa: BLE001
            self.outbox.put(chat_id, f"No pude descargar el archivo: {redact(str(e))[:200]}")
            return
        rel = dest.relative_to(self.s.workspace_dir)
        objective = (caption or "Analizá este documento y resumí lo importante.") + \
            f"\n\n[Archivo recibido: '{rel}' — leelo con read_file]"
        jid = enqueue(self.db, chat_id, objective, origin="telegram")
        self.outbox.put(chat_id, f"📎 Archivo guardado. Trabajo #{jid} en cola.")

    # ------------------------------------------------------------------ comandos
    def command(self, chat_id: int, text: str) -> str:
        parts = text.split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        n = int(arg) if arg.isdigit() else None
        db = self.db
        if cmd in ("/start", "/ayuda", "/help"):
            return HELP
        if cmd == "/estado":
            act = db.all("SELECT id,status,substr(objective,1,60) o FROM jobs WHERE chat_id=? AND status IN "
                         "('queued','running','waiting_approval') ORDER BY id", (chat_id,))
            pend = db.all("SELECT a.id,a.tool FROM approvals a JOIN jobs j ON j.id=a.job_id "
                          "WHERE a.status='pending' AND j.chat_id=?", (chat_id,))
            lines = ["📊 Estado"]
            lines += [f"#{r['id']} [{r['status']}] {r['o']}" for r in act] or ["Sin trabajos activos."]
            if pend:
                lines.append("Aprobaciones pendientes: " + ", ".join(f"#{p['id']} ({p['tool']})" for p in pend))
            lines.append(f"Llamadas al modelo hoy: {self.llm.calls_today() if hasattr(self.llm, 'calls_today') else '?'}"
                         f"/{self.s.llm_daily_cap}")
            return "\n".join(lines)
        if cmd == "/tareas":
            rows = db.all("SELECT id,status,substr(objective,1,50) o FROM jobs WHERE chat_id=? ORDER BY id DESC LIMIT 10",
                          (chat_id,))
            return "\n".join(f"#{r['id']} [{r['status']}] {r['o']}" for r in rows) or "Sin trabajos."
        if cmd == "/ver" and n:
            r = db.one("SELECT * FROM jobs WHERE id=? AND chat_id=?", (n, chat_id))
            if not r:
                return "No existe."
            return f"#{n} [{r['status']}]\n{r['result'] or r['error'] or '(sin resultado aún)'}"
        if cmd == "/cancelar" and n:
            c = db.execute("UPDATE jobs SET status='cancelled', updated=? WHERE id=? AND chat_id=? AND status IN "
                           "('queued','running','waiting_approval')", (time.time(), n, chat_id)).rowcount
            db.execute("UPDATE approvals SET status='expired' WHERE job_id=? AND status='pending'", (n,))
            return f"Trabajo #{n} cancelado." if c else "No se pudo cancelar (inexistente o terminado)."
        if cmd == "/reintentar" and n:
            r = db.one("SELECT objective FROM jobs WHERE id=? AND chat_id=?", (n, chat_id))
            if not r:
                return "No existe."
            jid = enqueue(db, chat_id, r["objective"], origin=f"retry:{n}")
            return f"Reintento en cola como trabajo #{jid}."
        if cmd in ("/aprobar", "/rechazar") and n:
            return resolve_approval(db, n, cmd == "/aprobar", chat_id)
        if cmd == "/recordar" and arg:
            kind = "instruction" if re.match(r"(siempre|nunca|cuando)\b", arg, re.I) else "preference"
            mid = self.memory.save(arg, kind=kind, source="user", confidence="alta", chat_id=chat_id)
            return f"🧠 Guardado como mem#{mid} ({kind})."
        if cmd == "/memoria":
            mems = self.memory.search(arg, 15) if arg else self.memory.recent(15)
            if not mems:
                return "Memoria vacía o sin coincidencias."
            return "\n".join(f"mem#{m['id']} [{m['kind']}; {m['source']}] {m['content'][:200]}" for m in mems)
        if cmd == "/olvidar":
            if arg.lower() == "todo":
                k = self.memory.delete_all(chat_id)
                return f"🗑️ {k} memorias eliminadas."
            if n:
                return "🗑️ Eliminada." if self.memory.delete(n) else "No existe."
        if cmd == "/programar" and arg:
            m = re.match(r"(every:\d+|daily:\d{1,2}:\d{2})\s+(.+)", arg, re.S)
            if not m:
                return "Formato: /programar every:120 <objetivo>  o  /programar daily:08:30 <objetivo>"
            try:
                sid = add_schedule(db, chat_id, m.group(2), m.group(1), self.s.timezone)
            except ValueError as e:
                return f"No se pudo programar: {e}"
            return f"⏰ Tarea recurrente #{sid} creada ({m.group(1)}, zona {self.s.timezone})."
        if cmd == "/programas":
            rows = db.all("SELECT * FROM schedules WHERE chat_id=? AND enabled=1", (chat_id,))
            return "\n".join(f"#{r['id']} {r['spec']} → {r['objective'][:60]} (próxima: "
                             f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(r['next_run']))})"
                             for r in rows) or "Sin tareas programadas."
        if cmd == "/desprogramar" and n:
            c = db.execute("UPDATE schedules SET enabled=0 WHERE id=? AND chat_id=?", (n, chat_id)).rowcount
            return "Desprogramada." if c else "No existe."
        if cmd == "/uso":
            rows = db.all("SELECT * FROM usage ORDER BY day DESC LIMIT 14")
            if not rows:
                return "Sin consumo registrado."
            return "📈 Consumo (día, proveedor, llamadas, tokens entrada/salida, errores)\n" + "\n".join(
                f"{r['day']} {r['provider']}: {r['calls']} | {r['prompt_tokens']}/{r['completion_tokens']} | {r['errors']}"
                for r in rows)
        return "Comando no reconocido o incompleto. /ayuda"

    # ------------------------------------------------------------------ procesamiento
    def process_jobs(self, deadline: float) -> dict:
        stats = {"done": 0, "failed": 0, "waiting": 0, "requeued": 0, "quota": False}
        while time.time() < deadline:
            job = claim_next(self.db)
            if not job:
                break
            try:
                out = self.agent.run_job(job)
            except QuotaExhausted as e:
                stats["quota"] = True
                self.db.log("warn", "quota", str(e)[:300], job["id"])
                if not self.db.kv_get(f"quota_warned:{time.strftime('%Y-%m-%d')}"):
                    self.db.kv_set(f"quota_warned:{time.strftime('%Y-%m-%d')}", "1")
                    self.outbox.put(job["chat_id"], "⏸️ Cuota gratuita del modelo agotada por hoy. "
                                    "Los trabajos quedan en cola y se reanudan automáticamente.")
                break
            except Exception as e:  # noqa: BLE001 - error inesperado: registrar y seguir con otros
                self.db.log("error", "job_crash", f"{type(e).__name__}: {redact(str(e))[:300]}", job["id"])
                st = "failed" if job["attempts"] >= self.s.budget.max_attempts else "queued"
                self.db.execute("UPDATE jobs SET status=?, lease_until=0, error=? WHERE id=?",
                                (st, f"{type(e).__name__}: {redact(str(e))[:500]}", job["id"]))
                if st == "failed":
                    self.outbox.put(job["chat_id"], f"❌ Trabajo #{job['id']} falló por un error interno "
                                    f"({type(e).__name__}). Quedó registrado.")
                stats["failed" if st == "failed" else "requeued"] += 1
                continue
            if out.status in ("done", "failed"):
                stats[out.status] += 1
                header = f"🧾 Trabajo #{out.job_id}\n"
                self.outbox.put(job["chat_id"], header + out.text)
                for f in out.files[:10]:
                    self.outbox.put(job["chat_id"], f"#{out.job_id}: {Path(f).name}", file_path=f)
            elif out.status == "waiting_approval":
                stats["waiting"] += 1
            else:
                stats["requeued"] += 1
            self.outbox.flush()
        return stats

    def run_once(self, max_seconds: int | None = None) -> dict:
        """Una pasada completa (modo GitHub Actions/cron)."""
        t0 = time.time()
        deadline = t0 + (max_seconds or self.s.run_seconds)
        report = {"updates": 0, "scheduled": 0}
        self.memory.purge_expired()
        if self.messenger and hasattr(self.messenger, "get_updates"):
            try:
                offset = self.db.kv_get("tg_offset")
                ups = self.messenger.get_updates(int(offset) if offset else None, timeout=0)
                for u in ups:
                    self.handle_update(u)
                report["updates"] = len(ups)
            except Exception as e:  # noqa: BLE001
                self.db.log("error", "telegram_poll", redact(str(e))[:300])
                report["poll_error"] = type(e).__name__
        self.outbox.flush()
        report["scheduled"] = len(run_due_schedules(self.db, self.s.timezone))
        report.update(self.process_jobs(deadline))
        sent, failed = self.outbox.flush()
        report.update({"sent": sent, "send_failed": failed, "seconds": round(time.time() - t0, 1)})
        if report["updates"] or report["scheduled"] or report["done"] or report["failed"] or report.get("poll_error"):
            self.db.log("info", "run_once", report)  # corridas vacías no modifican el estado
        self.maintenance()
        return report

    def maintenance(self) -> None:
        """Retención mínima de datos: evita crecimiento ilimitado del estado."""
        now = time.time()
        if float(self.db.kv_get("last_maintenance", "0") or 0) > now - 86400:
            return
        d = self.db
        d.execute("DELETE FROM events WHERE ts < ?", (now - 30 * 86400,))
        d.execute("DELETE FROM processed_updates WHERE ts < ?", (now - 14 * 86400,))
        d.execute("DELETE FROM outbox WHERE status!='pending' AND created < ?", (now - 7 * 86400,))
        d.execute("UPDATE jobs SET messages=NULL WHERE status IN ('done','failed','cancelled') AND updated < ?",
                  (now - 30 * 86400,))
        d.execute("DELETE FROM usage WHERE day < date('now','-90 day')")
        # Espacios de trabajo de trabajos terminados hace > 7 días y archivos compartidos > 14 días
        import shutil
        for r in d.all("SELECT id FROM jobs WHERE status IN ('done','failed','cancelled') AND updated < ?",
                       (now - 7 * 86400,)):
            shutil.rmtree(self.s.workspace_dir / f"trabajo_{r['id']}", ignore_errors=True)
        shared = self.s.workspace_dir / "compartido"
        if shared.exists():
            for p in shared.iterdir():
                if p.is_file() and p.stat().st_mtime < now - 14 * 86400:
                    p.unlink(missing_ok=True)
        d.kv_set("last_maintenance", str(now))
        d.conn.execute("VACUUM")

    def poll_forever(self, run_slice: int = 120, after_cycle=None) -> None:
        """Modo continuo (PC o Android/Termux): respuesta en segundos mientras esté encendido."""
        while True:
            try:
                offset = self.db.kv_get("tg_offset")
                for u in self.messenger.get_updates(int(offset) if offset else None, timeout=25):
                    self.handle_update(u)
                self.outbox.flush()
                run_due_schedules(self.db, self.s.timezone)
                self.process_jobs(time.time() + run_slice)
                self.outbox.flush()
                self.maintenance()
                if after_cycle:
                    after_cycle()  # p. ej. subir el estado cifrado a GitHub
            except KeyboardInterrupt:
                return
            except Exception as e:  # noqa: BLE001
                self.db.log("error", "poll_loop", redact(str(e))[:300])
                time.sleep(10)
