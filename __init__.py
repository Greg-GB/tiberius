"""tiberius — local memory engine: structured facts, entity graph, trust scoring,
HRR-based compositional retrieval over SQLite (+FTS5).

Harness-agnostic core (``store``, ``retrieval``, ``holographic``) + one adapter per harness:
``provider`` (Hermes MemoryProvider) and ``cli`` (any other harness, via subprocess).

Hermes plugin entry: the loader fetches ``register`` (and friends) through PEP 562
``__getattr__`` so importing this package outside Hermes — e.g. ``python3 -m tiberius.cli``
in a bare container — never pulls in Hermes core.
"""
from __future__ import annotations

__all__ = ["register", "TiberiusMemoryProvider", "_load_plugin_config"]


def __getattr__(name: str):
    if name in __all__:
        from . import provider
        value = getattr(provider, name)
        globals()[name] = value  # cache; PEP 562 lookup runs once per name
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
