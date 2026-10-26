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
