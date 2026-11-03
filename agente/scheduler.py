"""Cola de trabajos con arrendamiento (lease), deduplicación y tareas recurrentes."""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from zoneinfo import ZoneInfo

from .db import DB

MAX_SCHEDULES = 10
MIN_INTERVAL_MIN = 60
LEASE_SECONDS = 900


def enqueue(db: DB, chat_id: int, objective: str, origin: str = "user", dedupe_key: str | None = None) -> int | None:
    """Crea un trabajo. Si dedupe_key ya existe, no duplica (devuelve None)."""
    now = time.time()
    try:
        cur = db.execute(
            "INSERT INTO jobs(chat_id,objective,status,origin,dedupe_key,counters,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, objective, "queued", origin, dedupe_key, json.dumps({}), now, now),
        )
    except Exception as e:  # sqlite3.IntegrityError por dedupe_key
        if "UNIQUE" in str(e):
            return None
        raise
    db.log("info", "job_enqueued", {"origin": origin}, cur.lastrowid)
    return cur.lastrowid


def claim_next(db: DB) -> dict | None:
    """Toma el siguiente trabajo ejecutable. Recupera trabajos 'running' con lease vencido
    (ejecución interrumpida) para reanudarlos desde su último checkpoint."""
    now = time.time()
    row = db.one(
        "SELECT * FROM jobs WHERE (status='queued') OR (status='running' AND lease_until < ?) "
        "ORDER BY id LIMIT 1", (now,))
    if not row:
        return None
    upd = db.execute(
        "UPDATE jobs SET status='running', lease_until=?, attempts=attempts+1, updated=? "
        "WHERE id=? AND (status='queued' OR (status='running' AND lease_until < ?))",
        (now + LEASE_SECONDS, now, row["id"], now))
    if upd.rowcount != 1:
        return None
    if row["status"] == "running":
        db.log("warn", "job_recovered", "lease vencido: se reanuda desde checkpoint", row["id"])
    return dict(db.one("SELECT * FROM jobs WHERE id=?", (row["id"],)))


def parse_spec(spec: str) -> tuple[str, int | tuple[int, int]]:
    spec = spec.strip().lower()
    m = re.fullmatch(r"every:(\d+)", spec)
    if m:
        mins = int(m.group(1))
        if mins < MIN_INTERVAL_MIN:
            raise ValueError(f"intervalo mínimo {MIN_INTERVAL_MIN} minutos (protege la cuota gratuita)")
        return "every", mins
    m = re.fullmatch(r"daily:(\d{1,2}):(\d{2})", spec)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if h > 23 or mi > 59:
            raise ValueError("hora inválida")
        return "daily", (h, mi)
    raise ValueError("formato: 'every:<minutos>' o 'daily:HH:MM'")


def next_run_after(spec: str, tz: str, after: float) -> float:
    kind, val = parse_spec(spec)
    if kind == "every":
        return after + val * 60
    h, mi = val
    zone = ZoneInfo(tz)
    local = dt.datetime.fromtimestamp(after, zone)
    cand = local.replace(hour=h, minute=mi, second=0, microsecond=0)
    if cand.timestamp() <= after:
        cand = cand + dt.timedelta(days=1)
    return cand.timestamp()


def add_schedule(db: DB, chat_id: int, objective: str, spec: str, tz: str) -> int:
    parse_spec(spec)
    n = db.one("SELECT COUNT(*) c FROM schedules WHERE enabled=1")["c"]
    if n >= MAX_SCHEDULES:
        raise ValueError(f"máximo {MAX_SCHEDULES} tareas recurrentes activas")
    now = time.time()
    cur = db.execute("INSERT INTO schedules(chat_id,objective,spec,next_run,created) VALUES(?,?,?,?,?)",
                     (chat_id, objective, spec, next_run_after(spec, tz, now), now))
    return cur.lastrowid


def run_due_schedules(db: DB, tz: str, now: float | None = None) -> list[int]:
    """Encola las tareas recurrentes vencidas. dedupe_key evita duplicados si una ejecución
    se repite (p. ej., dos corridas superpuestas o un reintento)."""
    now = now or time.time()
    created = []
    for s in db.all("SELECT * FROM schedules WHERE enabled=1 AND next_run <= ?", (now,)):
        key = f"sched:{s['id']}:{int(s['next_run'])}"
        jid = enqueue(db, s["chat_id"], s["objective"], origin=f"schedule:{s['id']}", dedupe_key=key)
        nxt = next_run_after(s["spec"], tz, max(now, s["next_run"]))
        db.execute("UPDATE schedules SET next_run=?, last_job=COALESCE(?, last_job) WHERE id=?",
                   (nxt, jid, s["id"]))
        if jid:
            created.append(jid)
    return created
