"""Read-only shared stores (docs/cross-store.md): core MemoryStore surface —
open, schema gate, write refusal, no side effects."""
import sqlite3

import pytest

from hermes_plugin_tiberius import store as tstore
from hermes_plugin_tiberius.retrieval import FactRetriever


def _seeded(tmp_path, name="shared.db", n=3):
    path = tmp_path / name
    s = tstore.MemoryStore(db_path=path)
    for i in range(n):
        s.add_fact(f"Zorbax fact number {i} about widget assemblies", category="project")
    s.close()
    return path


def test_read_only_opens_and_reads(tmp_path):
    rs = tstore.MemoryStore(db_path=_seeded(tmp_path), read_only=True)
    out = FactRetriever(store=rs).search("zorbax widget", limit=10)
    rs.close()
    assert len(out) == 3
    assert "score" in out[0]


def test_read_only_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        tstore.MemoryStore(db_path=tmp_path / "nope.db", read_only=True)
    assert not (tmp_path / "nope.db").exists()


def test_read_only_empty_file_incompatible(tmp_path):
    p = tmp_path / "empty.db"
    p.write_bytes(b"")
    with pytest.raises(tstore.StoreIncompatible):
        tstore.MemoryStore(db_path=p, read_only=True)


def test_read_only_requires_explicit_path():
    with pytest.raises(ValueError):
        tstore.MemoryStore(read_only=True)


def test_read_only_rejects_writes(tmp_path):
    path = _seeded(tmp_path)
    rs = tstore.MemoryStore(db_path=path, read_only=True)
    with pytest.raises(RuntimeError):
        rs.add_fact("must not land")
    rs.close()
    c = sqlite3.connect(path)
    n = c.execute("select count(*) from facts").fetchone()[0]
    c.close()
    assert n == 3


def test_read_only_creates_no_side_effect_files(tmp_path):
    path = _seeded(tmp_path)
    before = {f.name for f in tmp_path.iterdir()}
    rs = tstore.MemoryStore(db_path=path, read_only=True)
    FactRetriever(store=rs).search("zorbax", limit=1)
    after = {f.name for f in tmp_path.iterdir()}
    rs.close()
    # WAL leftovers from the writer are fine; the reader must create nothing new.
    assert all(name.endswith((".db-wal", ".db-shm")) for name in after - before)
