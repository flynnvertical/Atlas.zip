"""Cliente mínimo de la Bot API de Telegram (sin dependencias extra)."""
from __future__ import annotations

import os
from pathlib import Path

import requests

from .security import redact

MAX_LEN = 4000


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token: str, api_base: str | None = None, timeout: int = 40):
        self.token = token
        self.base = (api_base or os.environ.get("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"{self.base}/bot{self.token}/{method}"

    def call(self, method: str, files=None, **params):
        try:
            if files:
                r = requests.post(self._url(method), data=params, files=files, timeout=self.timeout + 30)
            else:
                r = requests.post(self._url(method), json=params, timeout=self.timeout + 10)
        except requests.RequestException as e:
            # El token forma parte de la URL: nunca propagar el mensaje sin redactar.
            raise TelegramError(redact(f"{method}: {type(e).__name__}: {e}").replace(self.token, "[TOKEN]")) from None
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code} sin JSON") from None
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('error_code')} {data.get('description', '')[:200]}")
        return data["result"]

    def get_updates(self, offset: int | None, timeout: int = 0) -> list[dict]:
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params)

    def send_message(self, chat_id: int, text: str) -> None:
        text = text or "(vacío)"
        parts = []
        while text:
            cut = text[:MAX_LEN]
            if len(text) > MAX_LEN:
                nl = cut.rfind("\n")
                if nl > MAX_LEN // 2:
                    cut = cut[:nl]
            parts.append(cut)
            text = text[len(cut):]
        for p in parts:
            self.call("sendMessage", chat_id=chat_id, text=p, disable_web_page_preview=True)

    def send_document(self, chat_id: int, path: str, caption: str = "") -> None:
        p = Path(path)
        with p.open("rb") as fh:
            self.call("sendDocument", files={"document": (p.name, fh)}, chat_id=chat_id, caption=caption[:900])

    def download_file(self, file_id: str, dest: Path, max_bytes: int = 20_000_000) -> Path:
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("archivo demasiado grande")
        url = f"{self.base}/file/bot{self.token}/{info['file_path']}"
        r = requests.get(url, timeout=120)
        if r.status_code != 200:
            raise TelegramError(f"descarga HTTP {r.status_code}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        return dest
