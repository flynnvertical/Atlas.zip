"""Memoria persistente con búsqueda de texto completo (SQLite FTS5, BM25).

Principios:
- Toda memoria tiene origen (user/agent) y confianza; las del agente NO son hechos.
- Las correcciones no borran: marcan la versión anterior como reemplazada.
- El usuario puede borrar cualquier memoria (/olvidar) o todas (/olvidar todo).
"""
from __future__ import annotations

import re
import time

from .db import DB

KINDS = {"preference", "instruction", "project", "result", "experience", "fact"}


def _fts_query(text: str) -> str:
    words = re.findall(r"\w{2,}", text.lower())
    stop = {"de", "la", "el", "los", "las", "que", "en", "un", "una", "y", "o", "a", "por",
            "para", "con", "del", "al", "se", "es", "mi", "me", "lo", "the", "and", "of"}
    words = [w for w in words if w not in stop][:12]
    return " OR ".join(f'"{w}"' for w in words)


class Memory:
    def __init__(self, db: DB):
        self.db = db

    def save(self, content: str, kind: str = "fact", project: str = "", source: str = "agent",
             confidence: str = "media", chat_id: int = 0, ttl_days: float | None = None) -> int:
        content = content.strip()
        if not content:
            raise ValueError("contenido vacío")
        if len(content) > 2000:
            raise ValueError("memoria demasiado larga (máx 2000 caracteres): resumí antes de guardar")
        if kind not in KINDS:
            kind = "fact"
        # Evita duplicados exactos vigentes
        dup = self.db.one(
            "SELECT id FROM memories WHERE content=? AND project=? AND superseded_by IS NULL",
            (content, project),
        )
        if dup:
            return dup["id"]
        now = time.time()
        expires = now + ttl_days * 86400 if ttl_days else None
        cur = self.db.execute(
            "INSERT INTO memories(kind,project,content,source,confidence,chat_id,created,updated,expires)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, project, content, source, confidence, chat_id, now, now, expires),
        )
        return cur.lastrowid

    def correct(self, mem_id: int, new_content: str, source: str = "user") -> int:
        old = self.db.one("SELECT * FROM memories WHERE id=?", (mem_id,))
        if not old:
            raise KeyError(f"memoria {mem_id} inexistente")
        new_id = self.save(new_content, old["kind"], old["project"], source,
                           "alta" if source == "user" else "media", old["chat_id"])
        self.db.execute("UPDATE memories SET superseded_by=?, updated=? WHERE id=?",
                        (new_id, time.time(), mem_id))
        return new_id

    def delete(self, mem_id: int) -> bool:
        return self.db.execute("DELETE FROM memories WHERE id=?", (mem_id,)).rowcount > 0

    def delete_all(self, chat_id: int | None = None) -> int:
        if chat_id is None:
            return self.db.execute("DELETE FROM memories").rowcount
        return self.db.execute("DELETE FROM memories WHERE chat_id IN (?,0)", (chat_id,)).rowcount

    def purge_expired(self) -> int:
        return self.db.execute(
            "DELETE FROM memories WHERE expires IS NOT NULL AND expires < ?", (time.time(),)
        ).rowcount

    def search(self, query: str, limit: int = 6, project: str | None = None,
               include_superseded: bool = False) -> list[dict]:
        q = _fts_query(query)
        rows = []
        if q:
            sql = ("SELECT m.*, bm25(memories_fts) AS score FROM memories_fts "
                   "JOIN memories m ON m.id = memories_fts.rowid WHERE memories_fts MATCH ?")
            params: list = [q]
            if not include_superseded:
                sql += " AND m.superseded_by IS NULL"
            if project:
                sql += " AND m.project=?"
                params.append(project)
            sql += " ORDER BY score LIMIT ?"
            params.append(limit)
            rows = self.db.all(sql, tuple(params))
        return [dict(r) for r in rows]

    def recent(self, limit: int = 10, kinds: tuple[str, ...] | None = None) -> list[dict]:
        sql = "SELECT * FROM memories WHERE superseded_by IS NULL"
        params: tuple = ()
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params = tuple(kinds)
        sql += " ORDER BY updated DESC LIMIT ?"
        return [dict(r) for r in self.db.all(sql, params + (limit,))]

    def context_for(self, objective: str, limit: int = 8) -> str:
        """Bloque de contexto: preferencias/instrucciones vigentes + memorias relevantes."""
        seen: set[int] = set()
        lines: list[str] = []
        # Memoria personal = pocos registros: además de las coincidencias por texto, siempre se incluyen
        # las más recientes (la búsqueda léxica no une "vende" con "venta" ni "empresa" con "nombre").
        for m in (self.recent(6, ("preference", "instruction")) + self.search(objective, limit)
                  + self.recent(6)):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            age_days = int((time.time() - m["updated"]) / 86400)
            lines.append(
                f"- [mem#{m['id']} {m['kind']}{'/' + m['project'] if m['project'] else ''}; "
                f"origen={m['source']}; confianza={m['confidence']}; hace {age_days} d] {m['content']}"
            )
        if not lines:
            return ""
        return ("MEMORIA PERSISTENTE (puede estar desactualizada). origen=user: lo dijo el usuario (válido para "
                "responderle sobre sí mismo); origen=agent: conclusiones previas, no hechos verificados:\n" + "\n".join(lines[:limit + 4]))
