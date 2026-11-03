"""Keep the suite hermetic from the developer's installed SessionGraph settings.

Runtime configuration intentionally follows the user's Codex home and app-wide
pipeline template.  Tests must opt into those files explicitly; otherwise a
local ``cleanup_missing_chats`` or ``condensation`` setting changes fixtures and
makes results depend on the machine running the suite.
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_sessiongraph_home(tmp_path_factory, monkeypatch):
    codex_home = tmp_path_factory.mktemp('codex-home')
    runtime_home = tmp_path_factory.mktemp('sessiongraph-runtime')
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    monkeypatch.setenv('SESSIONGRAPH_RUNTIME_HOME', str(runtime_home))
