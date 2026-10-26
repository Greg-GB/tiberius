"""Test bootstrap.

The plugin imports Hermes core (``agent``, ``tools``, ``hermes_cli``) from a checkout: set
``HERMES_AGENT_REPO`` to your hermes-agent clone (default ``~/.hermes/hermes-agent``), or
``pip install -e`` it. This repo is loaded as the package ``hermes_plugin_tiberius`` — the same tree
``hermes plugins install`` copies to ``$HERMES_HOME/plugins/tiberius/``.
"""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

HERMES_AGENT_REPO = Path(os.environ.get("HERMES_AGENT_REPO") or Path.home() / ".hermes" / "hermes-agent").expanduser()
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(HERMES_AGENT_REPO) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_REPO))

PACKAGE = "hermes_plugin_tiberius"
if PACKAGE not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        PACKAGE, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)]
    )
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[PACKAGE] = _module
    _spec.loader.exec_module(_module)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Every test gets its own HERMES_HOME; nothing touches the real ~/.hermes."""
    home = tmp_path / "hermes_test"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _symlinks_supported(tmp_dir: Path) -> bool:
    target, link = tmp_dir / "target", tmp_dir / "link"
    target.write_text("x", encoding="utf-8")
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        return False
    return True


def pytest_configure(config):
    config.addinivalue_line("markers", "require_symlinks: skip when symbolic links cannot be created here")


def pytest_runtest_setup(item):
    if item.get_closest_marker("require_symlinks"):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            if not _symlinks_supported(Path(tmp)):
                pytest.skip("symbolic links are not supported here (Windows needs developer mode)")
