import copy
import json
import socket
import sqlite3
import tomllib

import pytest
from sessiongraph.core import init_project, digest
from sessiongraph.native_settings import handle


def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    config = init_project(root)
    raw = json.loads(config.read_text())
    raw["provider"].update(
        protocol="responses",
        model="old-model",
        endpoint="http://127.0.0.1:4317/openai/v1/responses",
    )
    config.write_text(json.dumps(raw))
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text(
        '[features]\nkeep=true\n[hooks.saved]\ntrusted_hash="untouched"\n'
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "projects.json").write_text(json.dumps({"projects": [{"root": str(root)}]}))
    source = tmp_path / "Gateway.app/Contents/Resources/sessiongraph"
    source.mkdir(parents=True)
    (source / "entrypoint.py").write_text("")
    args = dict(state=state, codex_home=home, source=source)
    return config, args, digest(str(root))[:20]


def test_status_is_lazy_readonly_and_never_opens_database_or_network(tmp_path, monkeypatch):
    config, args, pid = setup(tmp_path)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: pytest.fail("DB opened"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network"))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    r = handle({"action": "status", "project": pid}, **args)
    assert r["ok"] and r["model"] == "old-model" and r["model_calls"] == 0
    assert r["mcp_registered"] is False and r["hook_registered"] is False
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert "untouched" not in json.dumps(r)


def test_model_save_is_global_atomic_and_preserves_project_bytes(tmp_path):
    config, args, pid = setup(tmp_path)
    prior = json.loads(config.read_text())
    prior["provider"]["api_key"] = "NEVER_RETURN"
    config.write_text(json.dumps(prior))
    old = config.read_bytes()
    r = handle({"action": "save_model", "project": pid, "model": "new/model"}, **args)
    assert config.read_bytes() == old
    template = args['state'] / 'template.json'
    assert json.loads(template.read_text())['provider']['model'] == 'new/model'
    assert 'NEVER_RETURN' not in template.read_text()
    assert r["model"] == "new/model" and "NEVER_RETURN" not in json.dumps(r)
    handle({'action':'save_model', 'model':'next'}, **args)
    assert any(json.loads(p.read_text()).get('provider', {}).get('model') == 'new/model'
               for p in (args['state'] / 'backups').rglob('*template.json'))
    handle({'action':'save_model', 'model':'new/model'}, **args)
    assert (
        handle({"action": "save_model", "project": pid, "model": "new/model"}, **args)[
            "changed_count"
        ]
        == 0
    )


def test_hook_install_is_independent_idempotent_and_preserves_trust(tmp_path):
    config, args, pid = setup(tmp_path)
    home = args["codex_home"]
    toml = (home / "config.toml").read_bytes()
    (home / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "other-hook"}]}]}}
        )
    )
    r = handle({"action": "install_hooks", "project": pid}, **args)
    assert r["hook_registered"] and r["hook_enabled"] and not r["mcp_registered"]
    assert (home / "config.toml").read_bytes() == toml
    hooks = json.loads((home / "hooks.json").read_text())
    assert hooks["hooks"]["PreCompact"][0]["hooks"][0]["command"] == "other-hook"
    since = json.loads((home / "sessiongraph.json").read_text())["enabled_since"]
    assert handle({"action": "install_hooks", "project": pid}, **args)["changed_count"] == 0
    assert json.loads((home / "sessiongraph.json").read_text())["enabled_since"] == since


def test_mcp_install_does_not_install_or_enable_hooks(tmp_path):
    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    hook = b'{"hooks":{}}'
    (home / "hooks.json").write_bytes(hook)
    r = handle({"action": "install_mcp", "project": pid}, **args)
    assert r["mcp_registered"] and not r["hook_registered"] and not r["hook_enabled"]
    assert (home / "hooks.json").read_bytes() == hook
    assert handle({"action": "install_mcp", "project": pid}, **args)["changed_count"] == 0
    cfg = tomllib.loads((home / "config.toml").read_text())
    assert cfg["features"]["keep"] and cfg["hooks"]["saved"]["trusted_hash"] == "untouched"
    assert cfg["mcp_servers"]["sessiongraph"]["args"] == ["mcp"]


def test_custom_mcp_is_not_overwritten(tmp_path):
    _, args, pid = setup(tmp_path)
    p = args["codex_home"] / "config.toml"
    p.write_text('[mcp_servers.sessiongraph]\ncommand="custom"\n')
    before = p.read_bytes()
    with pytest.raises(ValueError):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert p.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "extract"},
        {"action": "save_model", "project": "a" * 20, "model": "ok"},
        {"action": "save_model", "project": "../../x", "model": "ok"},
        {"action": "status", "path": "/private"},
        {"action": "status", "model": "x"},
    ],
)
def test_rejects_untrusted_requests(tmp_path, payload):
    _, args, _ = setup(tmp_path)
    with pytest.raises(ValueError):
        handle(payload, **args)


@pytest.mark.parametrize("model", ["", " ", "x\nxx", "x\x00", "x" * 257, False])
def test_invalid_models(tmp_path, model):
    config, args, pid = setup(tmp_path)
    before = config.read_bytes()
    with pytest.raises(ValueError):
        handle({"action": "save_model", "project": pid, "model": model}, **args)
    assert config.read_bytes() == before


def test_refuses_symlink_project_config(tmp_path):
    config, args, pid = setup(tmp_path)
    other = tmp_path / "outside"
    config.rename(other)
    config.symlink_to(other)
    with pytest.raises(ValueError):
        handle({"action": "save_model", "project": pid, "model": "new"}, **args)


def test_missing_project_status_and_install_requirement(tmp_path):
    _, args, _ = setup(tmp_path)
    assert handle({"action": "status"}, **args)["model"] == ""
    with pytest.raises(ValueError):
        handle({"action": "install_hooks"}, **args)


def test_saved_model_is_used_by_real_responses_extractor(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_LLM_API_KEY", "fixture-only-no-network")
    from sessiongraph.core import load_config
    from sessiongraph.extraction import ResponsesExtractor

    config, args, pid = setup(tmp_path)
    monkeypatch.setenv('SESSIONGRAPH_RUNTIME_HOME', str(args['state']))
    handle({"action": "save_model", "project": pid, "model": "selected-model"}, **args)
    assert ResponsesExtractor(load_config(config)).model == "selected-model"


def test_missing_dependency_fails_without_installation(tmp_path, monkeypatch):
    import sessiongraph.native_settings as module

    _, args, pid = setup(tmp_path)
    monkeypatch.setattr(module.importlib.util, "find_spec", lambda _: None)
    with pytest.raises(ValueError, match="python_mcp_required"):
        handle({"action": "install_hooks", "project": pid}, **args)
    assert not (args["codex_home"] / "hooks.json").exists()


def test_model_save_supports_explicit_direct_endpoint_without_changing_provider(tmp_path):
    config, args, pid = setup(tmp_path)
    raw = json.loads(config.read_text())
    raw["provider"]["endpoint"] = "https://external.example/v1/responses"
    config.write_text(json.dumps(raw))
    result = handle({"action": "save_model", "project": pid, "model": "new"}, **args)
    assert result["ok"] and result["model_calls"] == 0
    assert json.loads(config.read_text()) == raw
    assert json.loads((args['state'] / 'template.json').read_text())['provider']['model'] == 'new'


@pytest.mark.parametrize("body", [b"not-json", b"x" * 4097, b"{}"])
def test_cli_errors_are_bounded_and_sanitized(tmp_path, monkeypatch, capsys, body):
    import io
    import sessiongraph.native_settings as module

    _, args, _ = setup(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(args["codex_home"]))
    monkeypatch.setattr(module.sys, "stdin", io.TextIOWrapper(io.BytesIO(body)))
    assert module.main(args["state"], args["source"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and result["model_calls"] == 0
    assert str(tmp_path) not in str(result)


def test_concurrent_model_edit_is_not_overwritten(tmp_path):
    from sessiongraph.gateway_install import apply_changes

    target = tmp_path / "config.json"
    target.write_text("external-change")
    with pytest.raises(ValueError, match="concurrent"):
        apply_changes(tmp_path, {target: "ours"}, {target: b"original"})
    assert target.read_text() == "external-change"


def test_does_not_overwrite_custom_options_on_owned_mcp(tmp_path):
    _, args, pid = setup(tmp_path)
    handle({"action": "install_mcp", "project": pid}, **args)
    config = args["codex_home"] / "config.toml"
    config.write_text(config.read_text().replace("tool_timeout_sec = 60", "tool_timeout_sec = 90"))
    before = config.read_bytes()
    with pytest.raises(ValueError, match="custom_mcp_registration"):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert config.read_bytes() == before


def test_dev_checkout_cannot_retarget_installed_launcher(tmp_path):
    _, args, pid = setup(tmp_path)
    args["source"] = tmp_path / "checkout/vendor/sessiongraph"
    with pytest.raises(ValueError, match="installed_app_required"):
        handle({"action": "install_mcp", "project": pid}, **args)


def test_mcp_installs_bounded_agents_guidance_with_backup_and_idempotence(tmp_path):
    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    agents = home / "AGENTS.md"
    original = b"# My rules\r\nKeep my instructions exactly.\r\n"
    agents.write_bytes(original)
    r = handle({"action": "install_mcp", "project": pid}, **args)
    assert r["mcp_guidance_installed"] and r["mcp_registered"]
    data = agents.read_bytes()
    assert data.startswith(original)
    assert data.count(b"<!-- SESSIONGRAPH_START -->") == 1
    assert b"session_recall" in data and b"session_expand" in data
    assert b"session_remember" in data and b"current project" in data
    assert len(data) - len(original) < 1800
    assert any(
        p.read_bytes() == original for p in (args["state"] / "backups").rglob("*") if p.is_file()
    )
    assert handle({"action": "install_mcp", "project": pid}, **args)["changed_count"] == 0
    assert agents.read_bytes() == data


def test_agents_managed_block_upgrade_preserves_surrounding_user_content(tmp_path):
    _, args, pid = setup(tmp_path)
    agents = args["codex_home"] / "AGENTS.md"
    agents.write_text(
        "Before\n<!-- SESSIONGRAPH_START -->\nold\n<!-- SESSIONGRAPH_END -->\nAfter\n"
    )
    handle({"action": "install_mcp", "project": pid}, **args)
    assert agents.read_text().startswith("Before\n<!-- SESSIONGRAPH_START -->")
    assert agents.read_text().endswith("<!-- SESSIONGRAPH_END -->\nAfter\n")
    assert "\nold\n" not in agents.read_text()


@pytest.mark.parametrize(
    "text",
    [
        "<!-- SESSIONGRAPH_START -->",
        "<!-- SESSIONGRAPH_END -->",
        "<!-- SESSIONGRAPH_END --><!-- SESSIONGRAPH_START -->",
        "<!-- SESSIONGRAPH_START --><!-- SESSIONGRAPH_START --><!-- SESSIONGRAPH_END -->",
    ],
)
def test_bad_agents_markers_do_not_partially_install(tmp_path, text):
    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    (home / "AGENTS.md").write_text(text)
    original = (home / "config.toml").read_bytes()
    with pytest.raises(ValueError, match="agents"):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert (home / "config.toml").read_bytes() == original
    assert (home / "AGENTS.md").read_text() == text
    assert not (args["state"] / "runtime.json").exists()


def test_agents_symlink_is_rejected_without_registration_changes(tmp_path):
    _, args, pid = setup(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text("user content")
    home = args["codex_home"]
    (home / "AGENTS.md").symlink_to(target)
    original = (home / "config.toml").read_bytes()
    with pytest.raises(ValueError):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert target.read_text() == "user content"
    assert (home / "config.toml").read_bytes() == original


def test_guidance_status_does_not_confuse_registration_or_hook_install(tmp_path):
    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    handle({"action": "install_mcp", "project": pid}, **args)
    (home / "AGENTS.md").unlink()
    status = handle({"action": "status", "project": pid}, **args)
    assert status["mcp_registered"] and not status["mcp_guidance_installed"]
    handle({"action": "install_hooks", "project": pid}, **args)
    assert not (home / "AGENTS.md").exists()


def test_agents_write_failure_rolls_back_registration(tmp_path, monkeypatch):
    import sessiongraph.gateway_install as gi

    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    before = (home / "config.toml").read_bytes()
    write = gi.atomic_write

    def fail_agents(path, content):
        if path.name == "AGENTS.md":
            raise OSError("fixture write failed")
        return write(path, content)

    monkeypatch.setattr(gi, "atomic_write", fail_agents)
    with pytest.raises(OSError):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert (home / "config.toml").read_bytes() == before
    assert not (home / "AGENTS.md").exists()
    assert not (args["state"] / "runtime.json").exists()


@pytest.mark.parametrize(
    "content",
    [b"# SessionGraph MCP\nmy custom instructions\n", b"\xff", b"x" * (2 * 1024 * 1024 + 1)],
)
def test_unmanaged_or_invalid_agents_is_not_overwritten(tmp_path, content):
    _, args, pid = setup(tmp_path)
    path = args["codex_home"] / "AGENTS.md"
    path.write_bytes(content)
    before = (args["codex_home"] / "config.toml").read_bytes()
    with pytest.raises((ValueError, UnicodeError)):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert path.read_bytes() == content
    assert (args["codex_home"] / "config.toml").read_bytes() == before
    assert handle({"action": "status", "project": pid}, **args)["mcp_guidance_installed"] is False


def test_concurrent_agents_edit_is_not_overwritten(tmp_path, monkeypatch):
    import sessiongraph.gateway_install as gi

    _, args, pid = setup(tmp_path)
    path = args["codex_home"] / "AGENTS.md"
    path.write_text("original")
    apply = gi.apply_changes

    def race(state, planned, expected_originals=None):
        path.write_text("concurrent user change")
        return apply(state, planned, expected_originals)

    monkeypatch.setattr(gi, "apply_changes", race)
    with pytest.raises(ValueError, match="concurrent"):
        handle({"action": "install_mcp", "project": pid}, **args)
    assert path.read_text() == "concurrent user change"
    assert not (args["state"] / "runtime.json").exists()


def test_guidance_is_short_project_scoped_and_omits_chat_parameter():
    from sessiongraph.gateway_install import AGENTS_GUIDANCE

    assert "chat_id" not in AGENTS_GUIDANCE and "chat ID" not in AGENTS_GUIDANCE
    assert len(AGENTS_GUIDANCE.encode()) < 850
    assert "current project" in AGENTS_GUIDANCE
    assert "Use the SessionGraph MCP tools to save and recall" in AGENTS_GUIDANCE
    assert "across chats in the current project." in AGENTS_GUIDANCE
    for name in (
        "session_recall",
        "session_expand",
        "session_search",
        "session_resume",
        "session_remember",
    ):
        assert name in AGENTS_GUIDANCE


def test_uninstall_hooks_preserves_mcp_trust_other_hooks_and_all_data(tmp_path):
    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    handle({"action": "install_mcp", "project": pid}, **args)
    handle({"action": "install_hooks", "project": pid}, **args)
    hooks_path = home / "hooks.json"
    hooks = json.loads(hooks_path.read_text())
    hooks["hooks"]["PreCompact"][0]["hooks"].append({"type": "command", "command": "other-hook"})
    hooks["other"] = {"keep": True}
    hooks_path.write_text(json.dumps(hooks))
    before = {
        p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and "backups" not in p.parts
    }
    result = handle({"action": "uninstall_hooks"}, **args)
    assert result["ok"] and result["model_calls"] == 0
    assert result["hook_events"] == 0 and not result["hook_enabled"]
    assert result["mcp_registered"] and result["mcp_guidance_installed"]
    remaining = json.loads(hooks_path.read_text())
    assert remaining["other"] == {"keep": True}
    assert remaining["hooks"]["PreCompact"][0]["hooks"] == [
        {"type": "command", "command": "other-hook"}
    ]
    for p, data in before.items():
        if p not in {hooks_path, home / "sessiongraph.json"}:
            assert p.read_bytes() == data
    assert handle({"action": "uninstall_hooks"}, **args)["changed_count"] == 0
    assert handle({"action": "install_hooks", "project": pid}, **args)["hook_registered"]


def test_uninstall_mcp_removes_only_owned_registration_and_guidance(tmp_path):
    _, args, pid = setup(tmp_path)
    home = args["codex_home"]
    handle({"action": "install_hooks", "project": pid}, **args)
    agents = home / "AGENTS.md"
    agents.write_text("# User rules\nDo not delete me.\n")
    handle({"action": "install_mcp", "project": pid}, **args)
    agents.write_text(agents.read_text() + "\n# After\nKeep this too.\n")
    before = {
        p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and "backups" not in p.parts
    }
    result = handle({"action": "uninstall_mcp"}, **args)
    assert result["ok"] and result["model_calls"] == 0
    assert not result["mcp_registered"] and not result["mcp_guidance_installed"]
    assert result["hook_registered"] and result["hook_enabled"]
    assert "mcp_servers" not in tomllib.loads((home / "config.toml").read_text())
    text = agents.read_text()
    assert text.startswith("# User rules\nDo not delete me.\n")
    assert text.endswith("\n# After\nKeep this too.\n")
    assert "SESSIONGRAPH" not in text
    for p, data in before.items():
        if p not in {agents, home / "config.toml"}:
            assert p.read_bytes() == data
    assert handle({"action": "uninstall_mcp"}, **args)["changed_count"] == 0
    assert handle({"action": "install_mcp", "project": pid}, **args)["mcp_guidance_installed"]


@pytest.mark.parametrize("action", ["uninstall_mcp", "uninstall_hooks"])
def test_uninstall_absent_is_noop_without_project_or_runtime(tmp_path, action):
    _, args, _ = setup(tmp_path)
    before = {
        p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.name != "install.lock"
    }
    result = handle({"action": action}, **args)
    assert result["changed_count"] == 0
    assert before == {
        p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file() and p.name != "install.lock"
    }


@pytest.mark.parametrize(
    "config", ['[mcp_servers.sessiongraph]\ncommand="other"\n', "owned_custom"]
)
def test_uninstall_refuses_custom_mcp_without_removing_guidance(tmp_path, config):
    _, args, pid = setup(tmp_path)
    handle({"action": "install_mcp", "project": pid}, **args)
    home = args["codex_home"]
    p = home / "config.toml"
    p.write_text(p.read_text() + 'env={CUSTOM="keep"}\n' if config == "owned_custom" else config)
    original = p.read_bytes()
    agents = (home / "AGENTS.md").read_bytes()
    with pytest.raises(ValueError):
        handle({"action": "uninstall_mcp"}, **args)
    assert p.read_bytes() == original
    assert (home / "AGENTS.md").read_bytes() == agents


def test_uninstall_malformed_guidance_does_not_remove_mcp(tmp_path):
    _, args, pid = setup(tmp_path)
    handle({"action": "install_mcp", "project": pid}, **args)
    p = args["codex_home"] / "AGENTS.md"
    p.write_text("<!-- SESSIONGRAPH_START -->broken")
    before = (args["codex_home"] / "config.toml").read_bytes()
    with pytest.raises(ValueError):
        handle({"action": "uninstall_mcp"}, **args)
    assert (args["codex_home"] / "config.toml").read_bytes() == before


def test_uninstall_failure_rolls_back_mcp_registration(tmp_path, monkeypatch):
    import sessiongraph.gateway_install as gi

    _, args, pid = setup(tmp_path)
    handle({"action": "install_mcp", "project": pid}, **args)
    home = args["codex_home"]
    before = (home / "config.toml").read_bytes()
    original = gi.atomic_write

    def fail_agents(path, content):
        if path.name == "AGENTS.md":
            raise OSError("test failure")
        return original(path, content)

    monkeypatch.setattr(gi, "atomic_write", fail_agents)
    with pytest.raises(OSError):
        handle({"action": "uninstall_mcp"}, **args)
    assert (home / "config.toml").read_bytes() == before


def test_hook_uninstall_does_not_follow_symlink(tmp_path):
    _, args, pid = setup(tmp_path)
    handle({"action": "install_hooks", "project": pid}, **args)
    home = args["codex_home"]
    hooks = home / "hooks.json"
    dest = tmp_path / "external.json"
    hooks.rename(dest)
    hooks.symlink_to(dest)
    before = dest.read_bytes()
    with pytest.raises(ValueError):
        handle({"action": "uninstall_hooks"}, **args)
    assert dest.read_bytes() == before


def test_hook_uninstall_failure_rolls_back_hooks_and_preserves_enabled(tmp_path, monkeypatch):
    import sessiongraph.gateway_install as gi

    _, args, pid = setup(tmp_path)
    handle({"action": "install_hooks", "project": pid}, **args)
    home = args["codex_home"]
    hooks = (home / "hooks.json").read_bytes()
    settings = (home / "sessiongraph.json").read_bytes()
    original = gi.atomic_write

    def fail_settings(path, content):
        if path.name == "sessiongraph.json":
            raise OSError("fixture write failed")
        return original(path, content)

    monkeypatch.setattr(gi, "atomic_write", fail_settings)
    with pytest.raises(OSError):
        handle({"action": "uninstall_hooks"}, **args)
    assert (home / "hooks.json").read_bytes() == hooks
    assert (home / "sessiongraph.json").read_bytes() == settings


def test_uninstall_detects_concurrent_config_change(tmp_path, monkeypatch):
    import sessiongraph.gateway_install as gi

    _, args, pid = setup(tmp_path)
    handle({"action": "install_mcp", "project": pid}, **args)
    path = args["codex_home"] / "config.toml"
    original = gi.apply_changes

    def race(state, planned, expected_originals=None):
        path.write_text(path.read_text() + "\n# concurrent user edit\n")
        return original(state, planned, expected_originals)

    monkeypatch.setattr(gi, "apply_changes", race)
    with pytest.raises(ValueError, match="concurrent"):
        handle({"action": "uninstall_mcp"}, **args)
    assert path.read_text().endswith("# concurrent user edit\n")
    assert (args["codex_home"] / "AGENTS.md").exists()


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_guidance_reinstall_does_not_accumulate_blank_lines(newline):
    from sessiongraph.gateway_install import (
        AGENTS_GUIDANCE, merge_agents_guidance, remove_agents_guidance,
    )

    body = "# User rules" + newline + "Keep this."
    installed = merge_agents_guidance(body + newline * 6)
    assert installed == body + newline * 2 + AGENTS_GUIDANCE + "\n"
    for _ in range(5):
        installed_again = merge_agents_guidance(remove_agents_guidance(installed))
        assert installed_again == installed
        assert merge_agents_guidance(installed) == installed
    old = body + newline * 6 + AGENTS_GUIDANCE + "\n# After\n"
    assert merge_agents_guidance(old) == body + newline * 2 + AGENTS_GUIDANCE + "\n# After\n"


def test_guidance_formats_tools_and_parameters_as_inline_code():
    from sessiongraph.gateway_install import AGENTS_GUIDANCE

    for token in (
        "session_recall", "session_expand(source.entry_id)",
        'session_search(mode="raw")', "session_resume", "session_remember",
        "source_entry_id", "source_quote",
    ):
        assert f"`{token}`" in AGENTS_GUIDANCE


def test_graph_v2_opt_in_preserves_provider_and_never_opens_db(tmp_path,monkeypatch):
    config,args,pid=setup(tmp_path)
    prior=json.loads(config.read_text())
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:pytest.fail('DB opened by settings'))
    r=handle({'action':'enable_graph','project':pid},**args)
    assert r['graph_v2_enabled'] and r['model_calls']==0
    current=json.loads(config.read_text())
    assert current['provider']==prior['provider']
    assert 'jev' not in current['graph_v2']
    assert r['judgment_backend']=='selected_model'
    assert not handle({'action':'disable_graph','project':pid},**args)['graph_v2_enabled']


def test_status_reports_local_credential_presence_without_secret(tmp_path, monkeypatch):
    config, args, pid = setup(tmp_path)
    path = args["state"] / "credentials/jev.json"
    path.parent.mkdir(mode=0o700)
    path.write_text('{"api_key":"synthetic-never-return"}')
    path.chmod(0o600)
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("network"))
    result = handle({"action": "status", "project": pid}, **args)
    assert result["jev_configured"] is True and result["model_calls"] == 0
    assert "synthetic-never-return" not in json.dumps(result)
    path.unlink()
    assert handle({"action": "status", "project": pid}, **args)["jev_configured"] is False


def test_maintenance_is_explicit_scoped_and_model_free(tmp_path, monkeypatch):
    from sessiongraph.core import Store,load_config
    config,args,pid=setup(tmp_path)
    Store(load_config(config)).close()
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('network'))
    result=handle({'action':'storage_preview','project':pid},**args)
    assert result['maintenance']['before']['database_bytes']>0
    assert result['maintenance']['model_calls']==0
    result=handle({'action':'storage_compact','project':pid},**args)
    assert result['maintenance']['integrity']=='ok'
    result=handle({'action':'clean_notices','project':pid},**args)
    assert result['maintenance']['removed']==0
    with pytest.raises(ValueError,match='select_initialized_project'):
        handle({'action':'storage_compact'},**args)
    with pytest.raises(ValueError,match='unexpected_model'):
        handle({'action':'storage_compact','project':pid,'model':'no'},**args)


@pytest.mark.parametrize('action',['graph_preview','graph_assess','graph_compact','graph_undo'])
def test_retired_graph_actions_rejected_before_project_lookup_or_network(tmp_path,monkeypatch,action):
    config,args,pid=setup(tmp_path)
    before=config.read_bytes()
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('network'))
    monkeypatch.setattr('sessiongraph.native_settings._project',lambda *a:pytest.fail('project opened'))
    with pytest.raises(ValueError,match='invalid_action'):
        handle({'action':action,'project':pid},**args)
    assert config.read_bytes()==before


@pytest.mark.parametrize('field',['token','run_id'])
def test_retired_receipt_fields_rejected(tmp_path,field):
    _,args,pid=setup(tmp_path)
    with pytest.raises(ValueError,match='invalid_request'):
        handle({'action':'status','project':pid,field:'a'*64},**args)
