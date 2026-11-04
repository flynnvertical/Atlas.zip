"""Sincronización del estado cifrado con una rama de GitHub usando la API REST (sin Actions).

Permite ejecutar el agente en cualquier host efímero gratuito (Render, Koyeb, una PC, Termux) sin
perder memoria ni trabajos: al iniciar descarga el estado y, tras cada cambio, lo vuelve a subir.
Requiere AGENTE_STATE_REPO (owner/repo), AGENTE_STATE_GH_TOKEN (token *fine-grained* con permiso
Contents: read & write SÓLO sobre ese repo) y AGENTE_STATE_KEY.
"""
from __future__ import annotations

import base64
import os
import threading
from pathlib import Path

import requests

from .security import redact
from .state_store import digest_dir, pack_dir, unpack_dir

API = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
MAX_SYNC_BYTES = 40_000_000


class SyncError(Exception):
    pass


class GitHubStateSync:
    def __init__(self, repo: str, token: str, key: str, data_dir: Path, branch: str = "agent-state",
                 path: str = "estado.enc", api: str | None = None):
        self.repo, self.token, self.key = repo, token, key
        self.data_dir, self.branch, self.path = Path(data_dir), branch, path
        self.api = (api or API).rstrip("/")
        self._sha: str | None = None
        self._digest: str | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, data_dir: Path) -> "GitHubStateSync | None":
        repo, tok, key = (os.environ.get(k) for k in ("AGENTE_STATE_REPO", "AGENTE_STATE_GH_TOKEN", "AGENTE_STATE_KEY"))
        if repo and tok and key:
            return cls(repo, tok, key, data_dir)
        return None

    def _h(self, raw: bool = False) -> dict:
        return {"Authorization": f"Bearer {self.token}", "X-GitHub-Api-Version": "2022-11-28",
                "Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}

    def _err(self, r: requests.Response, what: str) -> SyncError:
        return SyncError(redact(f"{what}: HTTP {r.status_code} {r.text[:200]}").replace(self.token, "[TOKEN]"))

    def _ensure_branch(self) -> None:
        r = requests.get(f"{self.api}/repos/{self.repo}/git/ref/heads/{self.branch}", headers=self._h(), timeout=30)
        if r.status_code == 200:
            return
        repo = requests.get(f"{self.api}/repos/{self.repo}", headers=self._h(), timeout=30)
        if repo.status_code != 200:
            raise self._err(repo, "leer repo")
        base = repo.json()["default_branch"]
        ref = requests.get(f"{self.api}/repos/{self.repo}/git/ref/heads/{base}", headers=self._h(), timeout=30)
        if ref.status_code != 200:
            raise self._err(ref, "leer rama base")
        c = requests.post(f"{self.api}/repos/{self.repo}/git/refs", headers=self._h(), timeout=30,
                          json={"ref": f"refs/heads/{self.branch}", "sha": ref.json()["object"]["sha"]})
        if c.status_code not in (201, 422):
            raise self._err(c, "crear rama de estado")

    def pull(self) -> bool:
        """Descarga y descifra el estado. Devuelve False si todavía no existe."""
        with self._lock:
            url = f"{self.api}/repos/{self.repo}/contents/{self.path}"
            meta = requests.get(url, headers=self._h(), params={"ref": self.branch}, timeout=60)
            if meta.status_code == 404:
                self._sha, self._digest = None, digest_dir(self.data_dir) if self.data_dir.exists() else None
                return False
            if meta.status_code != 200:
                raise self._err(meta, "leer estado")
            self._sha = meta.json()["sha"]
            raw = requests.get(url, headers=self._h(raw=True), params={"ref": self.branch}, timeout=120)
            if raw.status_code != 200:
                raise self._err(raw, "descargar estado")
            tmp = self.data_dir.parent / ".estado_descargado.enc"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(raw.content)
            unpack_dir(tmp, self.data_dir, self.key)
            tmp.unlink(missing_ok=True)
            self._digest = digest_dir(self.data_dir)
            return True

    def push(self, force: bool = False) -> bool:
        """Cifra y sube el estado si cambió. Devuelve True si subió."""
        with self._lock:
            d = digest_dir(self.data_dir)
            if not force and d == self._digest:
                return False
            tmp = self.data_dir.parent / ".estado_subida.enc"
            size = pack_dir(self.data_dir, tmp, self.key)
            if size > MAX_SYNC_BYTES:
                raise SyncError(f"estado demasiado grande para sincronizar ({size // 1_000_000} MB)")
            self._ensure_branch()
            body = {"message": "estado", "branch": self.branch,
                    "content": base64.b64encode(tmp.read_bytes()).decode()}
            if self._sha:
                body["sha"] = self._sha
            r = requests.put(f"{self.api}/repos/{self.repo}/contents/{self.path}", headers=self._h(),
                             json=body, timeout=120)
            tmp.unlink(missing_ok=True)
            if r.status_code == 409 or (r.status_code == 422 and "sha" in r.text):
                raise SyncError("conflicto: otra instancia modificó el estado (ejecutá una sola instancia)")
            if r.status_code not in (200, 201):
                raise self._err(r, "subir estado")
            self._sha = r.json()["content"]["sha"]
            self._digest = d
            return True
