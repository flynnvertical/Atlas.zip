"""Configuración leída exclusivamente desde variables de entorno.

Nunca se escriben secretos en código ni en registros. Ver docs/CONFIGURACION.md.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Budget:
    """Límites duros por trabajo. Protegen cuotas gratuitas y evitan bucles."""
    max_llm_calls: int = 14
    max_tool_calls: int = 24
    max_seconds: int = 420
    max_revisions: int = 1
    max_attempts: int = 3  # reintentos del trabajo completo ante fallos transitorios


@dataclass
class Settings:
    data_dir: Path
    workspace_dir: Path
    db_path: Path
    telegram_token: str | None
    telegram_allowed: set[int]
    timezone: str
    llm_daily_cap: int
    run_seconds: int
    budget: Budget = field(default_factory=Budget)
    sandbox_network: bool = False
    context_chars: int = 24000

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.environ.get("AGENTE_DATA_DIR", "data")).resolve()
        ws = Path(os.environ.get("AGENTE_WORKSPACE", data_dir / "workspace")).resolve()
        allowed = set()
        for x in _list("TELEGRAM_ALLOWED_USERS"):
            try:
                allowed.add(int(x))
            except ValueError:
                pass
        b = Budget(
            max_llm_calls=_int("AGENTE_MAX_LLM_CALLS", 14),
            max_tool_calls=_int("AGENTE_MAX_TOOL_CALLS", 24),
            max_seconds=_int("AGENTE_MAX_SECONDS", 420),
        )
        return cls(
            data_dir=data_dir,
            workspace_dir=ws,
            db_path=data_dir / "agente.db",
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_allowed=allowed,
            timezone=os.environ.get("AGENTE_TZ", "America/Argentina/Buenos_Aires"),
            llm_daily_cap=_int("AGENTE_LLM_DAILY_CAP", 120),
            run_seconds=_int("AGENTE_RUN_SECONDS", 480),
            budget=b,
            context_chars=_int("AGENTE_CONTEXT_CHARS", 24000),
        )

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
