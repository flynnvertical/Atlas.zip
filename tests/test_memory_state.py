"""Memoria persistente entre sesiones y estado cifrado."""
import time

import pytest

from agente.db import DB
from agente.memory import Memory
from agente.state_store import StateKeyError, decrypt_file, encrypt_file

KEY = "clave-de-prueba-muy-larga-123"


def test_memory_search_ignores_accents(db):
    m = Memory(db)
    mid = m.save("Licitación de cables en Córdoba: cierre el 15/12", kind="project", project="licitaciones")
    m.save("Prefiero informes breves", kind="preference", source="user")
    res = m.search("licitacion cordoba")
    assert res and res[0]["id"] == mid
    assert m.search("cables", project="licitaciones")[0]["id"] == mid


def test_memory_correction_keeps_history(db):
    m = Memory(db)
    old = m.save("El proveedor X cotiza a 100", kind="fact")
    new = m.correct(old, "El proveedor X cotiza a 120", source="user")
    assert [r["id"] for r in m.search("proveedor cotiza")] == [new]
    hist = m.search("proveedor cotiza", include_superseded=True)
    assert {r["id"] for r in hist} == {old, new}


def test_memory_delete_all_and_expiry(db):
    m = Memory(db)
    a = m.save("temporal", ttl_days=1e-7)
    m.save("permanente")
    time.sleep(0.05)
    assert m.purge_expired() == 1 and not m.search("temporal")
    assert m.delete(a) is False
    assert m.delete_all() == 1


def test_memory_rejects_empty_and_huge(db):
    m = Memory(db)
    with pytest.raises(ValueError):
        m.save("   ")
    with pytest.raises(ValueError):
        m.save("x" * 3000)
    assert m.save("dup") == m.save("dup")  # sin duplicados


def test_memory_survives_sessions_with_encrypted_state(tmp_path):
    """Simula dos ejecuciones separadas (como dos corridas de GitHub Actions)."""
    db_path = tmp_path / "run1" / "agente.db"
    d1 = DB(db_path)
    Memory(d1).save("El usuario trabaja en ventas de insumos eléctricos", kind="preference", source="user")
    d1.close()
    enc = tmp_path / "repo" / "agente.db.enc"
    encrypt_file(db_path, enc, KEY)
    assert b"insumos" not in enc.read_bytes()  # ilegible sin clave
    db2_path = tmp_path / "run2" / "agente.db"
    decrypt_file(enc, db2_path, KEY)
    d2 = DB(db2_path)
    assert "insumos eléctricos" in Memory(d2).context_for("cotizar insumos eléctricos")
    d2.close()


def test_state_wrong_key_rejected(tmp_path):
    p = tmp_path / "a.db"
    DB(p).close()
    encrypt_file(p, tmp_path / "a.enc", KEY)
    with pytest.raises(StateKeyError):
        decrypt_file(tmp_path / "a.enc", tmp_path / "b.db", "otra-clave-distinta-0000")
    with pytest.raises(StateKeyError):
        encrypt_file(p, tmp_path / "c.enc", "corta")


def test_full_state_pack_unpack_via_cli(tmp_path):
    """Ciclo real del workflow: unpack -> ejecución -> digest -> pack, usando la CLI en subprocesos."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    from agente.state_store import digest_dir, pack_dir
    data = tmp_path / "data"
    d = DB(data / "agente.db")
    Memory(d).save("dato persistente", source="user")
    d.close()
    (data / "workspace" / "trabajo_3").mkdir(parents=True)
    (data / "workspace" / "trabajo_3" / "pendiente.txt").write_text("esperando aprobación")
    (data / "workspace" / "trabajo_3" / "_script_1.py").write_text("temporal")
    env = {**os.environ, "AGENTE_STATE_KEY": KEY, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    run = lambda *a: subprocess.run([sys.executable, "-m", "agente", "state", *a], env=env,
                                    capture_output=True, text=True, check=True).stdout.strip()
    before = run("digest", str(data))
    assert before == digest_dir(data)
    run("pack", str(data), str(tmp_path / "estado.enc"))
    assert b"persistente" not in (tmp_path / "estado.enc").read_bytes()
    restored = tmp_path / "data2"
    run("unpack", str(tmp_path / "estado.enc"), str(restored))
    assert run("digest", str(restored)) == before  # digest estable: no hay commits vacíos
    assert (restored / "workspace" / "trabajo_3" / "pendiente.txt").read_text() == "esperando aprobación"
    assert not (restored / "workspace" / "trabajo_3" / "_script_1.py").exists()
    assert Memory(DB(restored / "agente.db")).search("persistente")
    pack_dir(restored, tmp_path / "e2.enc", KEY)  # re-empaquetar funciona
