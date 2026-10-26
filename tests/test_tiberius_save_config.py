"""``save_config`` of the tiberius provider writes config.yaml through the canonical writer.

The provider used to ``yaml.dump`` straight over config.yaml, bypassing the config lock, the
managed-mode refusal and the atomic replace. Two contracts pin the canonical path: unrelated
sections survive a provider save, and a managed install refuses the write.
"""
from __future__ import annotations

import hermes_yaml as yaml

from hermes_plugin_tiberius import TiberiusMemoryProvider


def _provider():
    return TiberiusMemoryProvider(config={})


def test_save_config_merges_into_existing_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  default: keep-me\nmemory:\n  provider: tiberius\n")

    _provider().save_config({"db_path": "custom.db", "hrr_dim": "512"}, str(tmp_path))

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert raw["plugins"]["tiberius"] == {"db_path": "custom.db", "hrr_dim": "512"}
    assert raw["model"]["default"] == "keep-me"
    assert raw["memory"]["provider"] == "tiberius"


def test_save_config_respects_managed_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    before = "model:\n  default: managed\n"
    (tmp_path / "config.yaml").write_text(before)
    monkeypatch.setattr("hermes_cli.config.is_managed", lambda: True)
    monkeypatch.setattr("hermes_cli.config.managed_error", lambda *_a, **_k: None)

    _provider().save_config({"db_path": "custom.db"}, str(tmp_path))

    assert (tmp_path / "config.yaml").read_text() == before


def test_load_plugin_config_warns_and_returns_defaults_on_failure(monkeypatch, caplog):
    """A failing config load must log a warning (silent defaults are a debugging trap), not just vanish."""
    import logging

    import hermes_cli.config as hc
    from hermes_plugin_tiberius import _load_plugin_config

    monkeypatch.setattr(hc, "load_config_readonly", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.WARNING):
        assert _load_plugin_config() == {}
    assert any("tiberius plugin config load failed" in rec.message for rec in caplog.records)


def test_system_prompt_block_reads_count_under_store_lock():
    """Shared-connection rule: every connection access holds the store lock (the retriever methods all do;
    an unlocked COUNT here could interleave with a writer on the same sqlite3 connection)."""
    p = TiberiusMemoryProvider(config={})
    p.initialize(session_id="test")
    try:
        lock, real = p._store._lock, p._store._conn

        class _GuardedConn:  # sqlite3.Connection attrs are read-only; proxy instead of monkeypatch
            def execute(self, *a, **k):
                assert lock._is_owned(), "connection accessed without holding the store lock"
                return real.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(real, name)

        p._store._conn = _GuardedConn()
        assert "# Tiberius Memory" in p.system_prompt_block()
    finally:
        p.shutdown()
