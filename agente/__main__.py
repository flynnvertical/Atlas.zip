"""Punto de entrada: python -m agente <comando>."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .config import Settings


def _messenger(s: Settings):
    if not s.telegram_token:
        return None
    from .telegram import Telegram
    return Telegram(s.telegram_token)


def cmd_run_once(a) -> int:
    from .app import App
    s = Settings.from_env()
    app = App(s, messenger=_messenger(s))
    rep = app.run_once(a.seconds)
    print(json.dumps(rep, ensure_ascii=False))  # sólo métricas: nunca contenido de mensajes
    return 0


def cmd_poll(a) -> int:
    from .app import App
    from .state_sync import GitHubStateSync
    s = Settings.from_env()
    m = _messenger(s)
    if not m:
        print("Falta TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2
    sync = GitHubStateSync.from_env(s.data_dir)
    if sync:
        sync.pull()
        print("Estado restaurado desde GitHub.")
    try:
        m.call("deleteWebhook")  # el modo continuo usa getUpdates
    except Exception:  # noqa: BLE001
        pass
    print("Agente en modo continuo. Ctrl+C para salir.")
    App(s, messenger=m).poll_forever(after_cycle=sync.push if sync else None)
    return 0


def cmd_serve(a) -> int:
    from .server import serve
    from .state_sync import GitHubStateSync
    s = Settings.from_env()
    m = _messenger(s)
    if not m:
        print("Falta TELEGRAM_BOT_TOKEN", file=sys.stderr)
        return 2
    sync = GitHubStateSync.from_env(s.data_dir)
    if not sync:
        print("AVISO: sin AGENTE_STATE_REPO/AGENTE_STATE_GH_TOKEN/AGENTE_STATE_KEY el estado se pierde al reiniciar.")
    url = os.environ.get("AGENTE_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    httpd = serve(s, m, sync, public_url=url)
    print(f"Servidor escuchando en el puerto {httpd.server_address[1]}", flush=True)
    httpd.serve_forever()
    return 0


class _Console:
    def send_message(self, chat_id, text):
        print(f"\n--- mensaje ---\n{text}\n")

    def send_document(self, chat_id, path, caption=""):
        print(f"\n--- archivo --- {path} {caption}\n")


def cmd_ask(a) -> int:
    from .app import App
    from .scheduler import enqueue
    s = Settings.from_env()
    app = App(s, messenger=_Console())
    jid = enqueue(app.db, 0, a.objective, origin="cli")
    status = app.process_until_done(jid, time.time() + s.run_seconds)
    app.outbox.flush()
    print(json.dumps({"job": jid, "status": status}, ensure_ascii=False))
    return 0 if status in ("done", "waiting_approval") else 1


def cmd_approve(a) -> int:
    from .agent import resolve_approval
    from .db import DB
    s = Settings.from_env()
    print(resolve_approval(DB(s.db_path), a.id, a.approve))
    return 0


def cmd_state(a) -> int:
    from .state_store import decrypt_file, digest_dir, encrypt_file, pack_dir, unpack_dir
    key = os.environ.get("AGENTE_STATE_KEY", "")
    if a.action == "digest":
        print(digest_dir(Path(a.src)))
        return 0
    fn = {"encrypt": encrypt_file, "decrypt": decrypt_file, "pack": pack_dir, "unpack": unpack_dir}[a.action]
    res = fn(Path(a.src), Path(a.dst), key)
    print(f"{a.action}: ok" + (f" ({res})" if res is not None else ""))
    return 0


def cmd_doctor(a) -> int:
    from .llm import providers_from_env
    from .tools.code import network_isolation_available
    s = Settings.from_env()
    rep = {
        "proveedores_llm": [f"{p.name}:{p.model}" for p in providers_from_env()],
        "telegram_configurado": bool(s.telegram_token),
        "usuarios_autorizados": len(s.telegram_allowed),
        "vinculacion_con_codigo": len(os.environ.get("AGENTE_PAIRING_CODE", "")) >= 8,
        "aislamiento_red_sandbox": network_isolation_available(),
        "clave_estado": bool(os.environ.get("AGENTE_STATE_KEY")),
        "zona_horaria": s.timezone,
        "tope_diario_llm": s.llm_daily_cap,
    }
    if s.telegram_token and a.online:
        try:
            me = _messenger(s).call("getMe")
            rep["telegram_bot"] = "@" + me.get("username", "?")
        except Exception as e:  # noqa: BLE001
            rep["telegram_error"] = str(e)[:200]
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agente")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run-once", help="una pasada (cron / GitHub Actions)")
    r.add_argument("--seconds", type=int, default=None)
    r.set_defaults(f=cmd_run_once)
    sub.add_parser("poll", help="modo continuo con Telegram").set_defaults(f=cmd_poll)
    sub.add_parser("serve", help="servidor webhook (Render/Koyeb/PC)").set_defaults(f=cmd_serve)
    q = sub.add_parser("ask", help="ejecutar un objetivo desde consola")
    q.add_argument("objective")
    q.set_defaults(f=cmd_ask)
    for name, ok in (("approve", True), ("reject", False)):
        x = sub.add_parser(name)
        x.add_argument("id", type=int)
        x.set_defaults(f=cmd_approve, approve=ok)
    st = sub.add_parser("state", help="cifrar/descifrar estado")
    st.add_argument("action", choices=["encrypt", "decrypt", "pack", "unpack", "digest"])
    st.add_argument("src")
    st.add_argument("dst", nargs="?")
    st.set_defaults(f=cmd_state)
    d = sub.add_parser("doctor", help="diagnóstico de configuración")
    d.add_argument("--online", action="store_true")
    d.set_defaults(f=cmd_doctor)
    a = p.parse_args(argv)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
