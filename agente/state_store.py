"""Cifrado del estado (SQLite) para guardarlo en una rama git de un repositorio público.

Formato: b"AGS1" | salt(16) | nonce(12) | AES-256-GCM(zlib(db)).
Clave derivada con scrypt desde AGENTE_STATE_KEY (secreto de GitHub, nunca en el repo).
Sin la clave el archivo es ilegible; con una clave errónea el descifrado falla (autenticado).
"""
from __future__ import annotations

import os
import zlib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"AGS1"


class StateKeyError(Exception):
    pass


def _key(passphrase: str, salt: bytes) -> bytes:
    if not passphrase or len(passphrase) < 16:
        raise StateKeyError("AGENTE_STATE_KEY ausente o demasiado corta (mínimo 16 caracteres)")
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(nonce, zlib.compress(data, 9), MAGIC)
    return MAGIC + salt + nonce + ct


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if blob[:4] != MAGIC:
        raise StateKeyError("archivo de estado con formato desconocido")
    salt, nonce, ct = blob[4:20], blob[20:32], blob[32:]
    try:
        return zlib.decompress(AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, MAGIC))
    except Exception as e:  # InvalidTag
        raise StateKeyError("no se pudo descifrar el estado: clave incorrecta o archivo dañado") from e


def encrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    tmp.write_bytes(encrypt_bytes(Path(src).read_bytes(), passphrase))
    tmp.replace(dst)  # escritura atómica


def decrypt_file(src: Path, dst: Path, passphrase: str) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = decrypt_bytes(Path(src).read_bytes(), passphrase)
    if not data.startswith(b"SQLite format 3"):
        raise StateKeyError("el contenido descifrado no es una base SQLite válida")
    dst.write_bytes(data)


# -- paquete completo: base de datos + espacio de trabajo ------------------------------
import hashlib  # noqa: E402
import io  # noqa: E402
import tarfile  # noqa: E402

MAX_FILE_IN_STATE = 15_000_000
MAX_STATE_BYTES = 80_000_000  # GitHub rechaza archivos > 100 MB


def _iter_state_files(data_dir: Path):
    data_dir = Path(data_dir)
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file() or p.name.startswith("_script_") or p.suffix in (".tmp", ".enc"):
            continue
        if p.name.endswith(("-journal", "-wal", "-shm")):
            continue
        if p.stat().st_size > MAX_FILE_IN_STATE:
            continue  # archivos enormes no se persisten (se avisa en docs)
        yield p


def digest_dir(data_dir: Path) -> str:
    h = hashlib.sha256()
    for p in _iter_state_files(data_dir):
        h.update(str(p.relative_to(data_dir)).encode() + b"\0" + p.read_bytes() + b"\0")
    return h.hexdigest()


def pack_dir(data_dir: Path, dst: Path, passphrase: str) -> int:
    data_dir = Path(data_dir)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in _iter_state_files(data_dir):
            info = tarfile.TarInfo(str(p.relative_to(data_dir)))
            raw = p.read_bytes()
            info.size, info.mtime, info.mode = len(raw), 0, 0o600
            tar.addfile(info, io.BytesIO(raw))
    raw = buf.getvalue()
    if len(raw) > MAX_STATE_BYTES:
        raise StateKeyError(f"estado demasiado grande ({len(raw) // 1_000_000} MB)")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    tmp.write_bytes(encrypt_bytes(raw, passphrase))
    tmp.replace(dst)
    return dst.stat().st_size


def unpack_dir(src: Path, data_dir: Path, passphrase: str) -> int:
    raw = decrypt_bytes(Path(src).read_bytes(), passphrase)
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
        for m in tar.getmembers():
            target = (data_dir / m.name).resolve()
            if not m.isfile() or data_dir not in target.parents:
                continue  # sólo archivos regulares dentro del directorio
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(m).read())
            n += 1
    return n
