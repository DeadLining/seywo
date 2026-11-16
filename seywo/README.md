# Seywo — See Your Work

**Seywo 0.4.0** 是面向 Codex 的项目记忆插件。它按“项目 → Chat → Compact → 多轮对话”组织工作记录：在当前 Chat 展示一条紧凑主线，在项目内支持跨 Chat 检索，并按需定位原文证据。

Seywo 包含本地内核、STDIO MCP、Codex MCP App 面板和 Compact Hook。默认使用 Codex 当前登录、提供方和模型配置，不需要独立应用或额外 API Key；可选的 Responses API 配置保存在项目自己的 `.seywo/` 中。

## 安装

将仓库根目录作为插件市场添加到 Codex：

```sh
codex plugin marketplace add https://github.com/DeadLining/seywo.git
codex plugin add seywo@seywo-local
```

安装或更新后重新连接 Codex MCP。首次在项目中启用时，在当前 Chat 中调用 `seywo_initialize_project`，它会创建项目自己的 `.seywo/`、写入默认 `.gitignore` 并启用 Seywo Hook。启用不会导入旧 SessionGraph 数据，也不会自动扫描其他项目。

## 使用

打开 Seywo 面板后，插件会优先使用当前 Codex Chat 和当前项目，不需要手动填写项目路径。面板以主线为中心展示当前目标、未决问题、下一步和阶段标题；节点保持短标题，详情和来源按需打开。Compact 是处理触发器，不会作为一份重复的 Compact 日志展示。

MCP 检索默认覆盖当前项目已经导入的 Chat，也可以用 `chat_ids` 指定一个或多个 Chat。优先使用 `seywo_recall` 读取凝练事项，需要核对事实时再用 `seywo_search`、`seywo_expand` 或 `seywo_read_source` 定位来源。检索是本地、只读和无模型调用的；`seywo_remember` 是唯一的显式写入入口，并要求精确来源坐标。

## 模型和存储

Compact 使用同一套 `maintain → compact_pipeline`。规则先去除工具调用和工具结果，再把用户问题与助手回答按完整轮次送入一次有界模型请求；模型只返回短标题、状态变化、关系操作和来源索引，程序负责校验、合并、版本化和落库。过大的输入按既有边界分批处理。

Codex 是默认模型后端，直接复用宿主的登录和全局模型。自定义 Responses API 只能通过面板保存到当前项目的 `.seywo/credentials/`，凭据不会进入 Git，也不会写进 `config.json`。项目运行数据、索引、凭据和 Compact 记录都留在项目自己的 `.seywo/` 中。

Seywo 是独立的新存储和新索引，不兼容或融合 SessionGraph 旧数据。卸载插件不会删除项目数据；删除项目数据前请先按项目需要备份 `.seywo/`。

## 开发和验证

```sh
npm ci --prefix tools/seywo-ui
npm test --prefix tools/seywo-ui
npm run build --prefix tools/seywo-ui
python3 -m pytest -q -c seywo/pyproject.toml seywo/tests
python3 -m compileall -q seywo/src
```

发布包由 `scripts/package-seywo-plugin.py` 生成。插件运行时需要 macOS/Linux、Python 3.11+ 或 `uv`；Node/npm 只用于开发和构建 UI。完整安装、Hook 信任、模型、存储和验收边界见 [`docs/PLUGIN.md`](docs/PLUGIN.md) 与 [`docs/seywo-mcp.md`](docs/seywo-mcp.md)。
