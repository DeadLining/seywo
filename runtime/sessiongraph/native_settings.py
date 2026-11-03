"""Explicit app-wide provider settings and independent global registration.

Settings snapshots do not open a database. All native settings and storage
maintenance actions remain model-free; graph compaction actions are retired.
"""

from __future__ import annotations

import importlib.util
from .jev import saved_key_configured
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tomllib
import unicodedata
from urllib.parse import urlsplit

from .core import digest
from .gateway_install import apply_changes, install, agents_guidance_installed, uninstall
from .global_hooks import EVENTS, locked
from .provider_credentials import validate_endpoint, save_key
from .provider_settings import PROVIDER_FIELDS, effective_provider, read_template


def _json(path):
    if not path.exists():
        return {}
    if path.stat().st_size > 256 * 1024:
        raise ValueError("configuration_too_large")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("invalid_configuration")
    return value


def _project(state, project):
    if project is None:
        return None
    if not isinstance(project, str) or not re.fullmatch(r"[0-9a-fA-F]{20}", project):
        raise ValueError("invalid_project")
    for item in _json(state / "projects.json").get("projects", []):
        root = Path(item["root"])
        if not root.is_absolute():
            raise ValueError("invalid_project")
        root = root.resolve()
        if digest(str(root))[:20] != project:
            continue
        path = root / ".sessiongraph/config.json"
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise ValueError("unsafe_project_config")
        cfg = _json(path)
        if cfg.get("roots") != [str(root)] or cfg.get("project_id") != project:
            raise ValueError("project_identity_mismatch")
        return path
    raise ValueError("unknown_project")


def _snapshot(state, codex_home, config):
    provider = effective_provider(_json(config).get("provider", {}) if config else {}, state)
    launcher = state / "bin/sessiongraph"
    hooks = _json(codex_home / "hooks.json").get("hooks", {})
    command = shlex.quote(str(launcher)) + " hook-event"
    matches = [
        any(
            h.get("type") == "command" and h.get("command") == command
            for g in hooks.get(event, [])
            for h in g.get("hooks", [])
        )
        for event in EVENTS
    ]
    settings = _json(codex_home / "sessiongraph.json")
    path = codex_home / "config.toml"
    if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("configuration_too_large")
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    mcp = data.get("mcp_servers", {}).get("sessiongraph", {})
    runtime = _json(state / "runtime.json")
    return dict(
        ok=True,
        judgment_backend="selected_model",
        jev_configured=saved_key_configured(state),
        graph_v2_enabled=_json(config).get("graph_v2", {}).get("enabled", False) if config else False,
        model_calls=0,
        changed_count=0,
        model=provider.get("model", ""),
        endpoint=provider.get("endpoint", ""),
        api_key_env=provider.get("api_key_env", "GRAPH_LLM_API_KEY"),
        hook_event_count=len(EVENTS),
        runtime_installed=launcher.is_file() and Path(runtime.get("python", "")).is_file(),
        hook_registered=all(matches),
        hook_events=sum(matches),
        hook_enabled=settings.get("enabled") is True,
        mcp_registered=mcp.get("command") == str(launcher)
        and mcp.get("args") == ["mcp"]
        and mcp.get("enabled") is not False,
        mcp_guidance_installed=agents_guidance_installed(codex_home / "AGENTS.md"),
        message="",
    )


def handle(request, *, state: Path, codex_home: Path, source: Path):
    if not isinstance(request, dict) or set(request) - {"action", "project", "model", "endpoint", "api_key_env", "api_key"}:
        raise ValueError("invalid_request")
    action = request.get("action")
    if action not in {
        "status",
        "storage_preview",
        "storage_compact",
        "clean_notices",
        "clear_tasks",
        "save_model",
        "save_provider",
        "enable_graph",
        "disable_graph",
        "install_hooks",
        "install_mcp",
        "uninstall_hooks",
        "uninstall_mcp",
    }:
        raise ValueError("invalid_action")
    if action not in {"save_model", "save_provider"} and request.get("model") is not None:
        raise ValueError("unexpected_model")
    if action != "save_provider" and any(request.get(k) is not None for k in ("endpoint", "api_key_env", "api_key")):
        raise ValueError("unexpected_provider")
    removing = action in {"uninstall_hooks", "uninstall_mcp"}
    config = None if removing else _project(state, request.get("project"))
    if action == "status":
        return _snapshot(state, codex_home, config)
    if config is None and not removing and action not in {"save_model", "save_provider"}:
        raise ValueError("select_initialized_project")
    if action in {"storage_preview", "storage_compact", "clean_notices", "clear_tasks"}:
        from .maintenance import run
        maintenance = run(config, action)
        return dict(_snapshot(state, codex_home, config), maintenance=maintenance)
    if removing:
        change = uninstall(codex_home=codex_home, state=state, action=action)
    elif action in {"enable_graph", "disable_graph"}:
        with locked(config.with_suffix(".lock")):
            data = _json(config)
            data.setdefault("graph_v2", {})["enabled"] = action == "enable_graph"
            change = apply_changes(state, {config: json.dumps(data, ensure_ascii=False, indent=2) + "\n"})
    elif action in {"save_model", "save_provider"}:
        model = request.get("model")
        if (
            not isinstance(model, str)
            or not model.strip()
            or len(model) > 256
            or any(unicodedata.category(c) == "Cc" for c in model)
        ):
            raise ValueError("invalid_model")
        with locked(state / "install.lock"):
            target = state / "template.json"
            raw = read_template(state)
            before = target.read_bytes() if target.exists() else None
            if not raw:
                seed = _json(config) if config else {}
                raw = {"provider": {k: v for k, v in seed.get("provider", {}).items() if k in PROVIDER_FIELDS},
                       "extraction": seed.get("extraction", {"max_batch_chars":24000,
                           "context_entries":2, "max_input_chars":32000, "lease_seconds":600})}
                raw["provider"].setdefault("timeout_seconds", 90)
                raw["provider"].setdefault("max_output_tokens", 4000)
            if action == "save_provider":
                endpoint = validate_endpoint(request.get("endpoint"))
                key_env = request.get("api_key_env", "GRAPH_LLM_API_KEY")
                if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key_env):
                    raise ValueError("invalid_key_env")
                if request.get("api_key"):
                    save_key(state, endpoint, request["api_key"])
                raw.setdefault("provider", {}).update(protocol="responses", endpoint=endpoint, api_key_env=key_env, model=model.strip(), auth="bearer")
                change = apply_changes(state, {target: json.dumps(raw, ensure_ascii=False, indent=2) + "\n"}, {target: before})
            elif raw.get("provider", {}).get("protocol") != "responses":
                raise ValueError("requires_responses_project")
            elif raw["provider"].get("model") == model.strip():
                change = {"changed": []}
            else:
                raw["provider"]["model"] = model.strip()
                change = apply_changes(
                    state,
                    {target: json.dumps(raw, ensure_ascii=False, indent=2) + "\n"},
                    {target: before},
                )
    else:
        if sys.version_info < (3, 11) or importlib.util.find_spec("mcp") is None:
            raise ValueError("python_mcp_required")
        if source.parts[-3:] != ("Contents", "Resources", "sessiongraph"):
            raise ValueError("installed_app_required")
        if action == "install_mcp":
            path = codex_home / "config.toml"
            existing = (
                tomllib.loads(path.read_text()).get("mcp_servers", {}).get("sessiongraph", {})
                if path.exists()
                else {}
            )
            if existing.get("command") == str(state / "bin/sessiongraph") and (
                existing.get("args") != ["mcp"]
                or set(existing) - {"command", "args", "tool_timeout_sec"}
                or existing.get("tool_timeout_sec", 60) != 60
            ):
                raise ValueError("custom_mcp_registration")
        change = install(
            codex_home=codex_home,
            state=state,
            app=source.parents[2],
            source_root=_installed_runtime_source(state, source),
            python=Path(sys.executable),
            legacy=config.parent.parent / "sessiongraph",
            project_config=config,
            install_hooks=action == "install_hooks",
            install_mcp=action == "install_mcp",
        )
    result = _snapshot(state, codex_home, config)
    result["changed_count"] = len(change["changed"])
    result["message"] = {
        "enable_graph": "对象图谱 v2 已启用；只处理新增内容，历史数据保留。",
        "disable_graph": "已恢复旧抽取路径，v2 数据保留。",
        "save_model": "全局模型已保存，所有项目的后续抽取和判断共用此模型。",
        "save_provider": "全局模型配置已保存，所有项目的后续任务生效。",
        "install_hooks": "Hook 已注册；请在 Codex 中确认信任。",
        "install_mcp": "MCP 和使用指引已安装。",
        "uninstall_hooks": "Hook 已卸载，自动处理已停用；数据保留，进行中的任务不强制终止。",
        "uninstall_mcp": "MCP 注册和使用指引已移除；数据与 Hook 保留，已有连接需关闭或重连。",
    }[action]
    return result


def _installed_runtime_source(state, source):
    """Keep an install/reinstall from pinning future hooks to a mutable app bundle."""
    runtime = _json(state / "runtime.json")
    saved = Path(runtime.get("source", ""))
    try:
        relative = saved.relative_to(state / "runtimes")
    except ValueError:
        return source
    if len(relative.parts) == 1 and (saved / "entrypoint.py").is_file() and (saved / "sessiongraph/provider_credentials.py").is_file():
        return saved
    return source


MESSAGES = {
    "maintenance_busy": "项目正在处理批次，请在本轮完成后再压缩或清理。",
    "maintenance_fact_count_changed": "维护校验未通过，压缩事务已回滚；数据保留。",
    "maintenance_integrity_failed": "索引完整性检查未通过，压缩事务已回滚。",
    "invalid_agents_markers": "AGENTS.md 的 SessionGraph 标记不完整；未修改配置，请先检查该段落。",
    "custom_agents_guidance": "AGENTS.md 已有自定义 SessionGraph 段落；未覆盖，请先人工确认。",
    "unsafe_agents_file": "AGENTS.md 是符号链接；为保护原文件，未修改注册。",
    "agents_file_too_large": "AGENTS.md 过大；未修改注册，请先人工检查。",
    "installed_app_required": "请在已安装的 SessionGraph 应用中执行注册安装。",
    "custom_mcp_registration": "已有自定义 SessionGraph MCP 配置，未覆盖；请先人工确认。",
    "select_initialized_project": "请先选择一个已初始化的项目。",
    "requires_responses_project": "此项目未配置 Responses 抽取，请先保存模型端点。",
    "invalid_endpoint": "请输入完整 HTTPS /responses 端点；仅本机地址允许 HTTP。",
    "python_mcp_required": "需要 Python 3.11+ 和 mcp 依赖；不会自动下载软件。",
}


def main(state: Path, source: Path):
    try:
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError("request_too_large")
        result = handle(
            json.loads(raw),
            state=state,
            codex_home=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
            source=source,
        )
    except Exception as exc:
        message = MESSAGES.get(
            str(exc), "设置操作失败：请检查项目、运行时和注册配置；自定义注册不会被覆盖。"
        )
        result = dict(ok=False, model_calls=0, message=message)
    print(json.dumps(result, ensure_ascii=False))
    return 0
