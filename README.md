# Seywo

Seywo（See Your Work）是一个面向 Codex 的项目记忆插件：按项目和 Chat 维护对话主线、当前重点、支线与可回溯来源，并在 Compact 时使用 Codex 已有模型整理完整的用户-AI轮次。

## 在 Codex 中安装

本仓库同时提供插件市场清单和 Seywo 插件目录。将仓库添加为 Codex 插件市场后，安装 `seywo@seywo-local`：

```bash
codex plugin marketplace add https://github.com/DeadLining/seywo.git
codex plugin add seywo@seywo-local
```

在 Codex App 中，也可以通过“添加 → 添加插件市场”添加本仓库；稀疏路径使用 `.`。

Seywo 插件自带本地内核、MCP、面板和 Compact Hook，默认复用用户自己的 Codex 登录、provider 和模型，不需要独立 Seywo 应用或额外 API Key。macOS/Linux 需要 Python 3.11+ 或 `uv`。首次启用项目时会在项目目录创建 `.seywo/`，该目录默认被 Git 忽略。

详细安装、项目绑定、Hook 信任、模型和存储边界见 [`seywo/docs/PLUGIN.md`](seywo/docs/PLUGIN.md)。

## 仓库结构

- `.agents/plugins/marketplace.json`：Codex 插件市场清单
- `seywo/`：Seywo 插件本体、本地运行时、MCP、Hook、面板和测试
- `runtime/`、`src/`、`src-tauri/`：桌面端集成与兼容运行代码
- `docs/`：设计、契约和验收记录

## 开发和验证

```bash
python3 -m pytest -q -c seywo/pyproject.toml seywo/tests
npm test
```

插件的发布包由 `scripts/package-seywo-plugin.py` 生成。项目运行数据、Chat 索引、模型凭据和本地验收产物不属于仓库发布内容。
