# Seywo — See Your Work

**Seywo 0.5.3** 是面向 Codex 的项目记忆插件。它按“项目 → Chat → Compact → 多轮对话”组织工作记录：在当前 Chat 展示一条紧凑主线，在项目内支持跨 Chat 检索，并按需定位原文证据。

Seywo 包含本地内核、STDIO MCP、Codex MCP App 面板和 Compact Hook。默认使用 Codex 当前登录、提供方和模型配置，不需要独立应用或额外 API Key；可选的 Responses API 配置保存在项目自己的 `.seywo/` 中。

## 安装

将仓库根目录作为插件市场添加到 Codex：

```sh
codex plugin marketplace add https://github.com/DeadLining/seywo.git
codex plugin add seywo@seywo-local
```

安装或更新后重新连接 Codex MCP。插件只需全局安装一次，每个项目首次打开 Seywo 面板时自动按需初始化，无需在对话中输入“启用 Seywo”。初始化只在当前 Chat 明确绑定的项目中创建 `.seywo/.gitignore`、`config.json` 和空索引数据库，并启用项目 Hook；不会导入历史对话、调用模型或扫描其他项目。已有配置、project_id 和数据库直接复用；模型选择统一读取用户级设置。无法确定当前项目时仍显示项目选择器；失败时显示原因并提供重试。原生 Hook 仍遵守宿主的信任机制。

## 使用

打开 Seywo 面板后，插件会优先使用当前 Codex Chat 和当前项目，不需要手动填写项目路径。面板以主线为中心展示当前目标、未决问题、下一步和阶段标题；节点保持短标题，详情和来源按需打开。Compact 是处理触发器，不会作为一份重复的 Compact 日志展示。

MCP 检索默认覆盖当前项目已经导入的 Chat，也可以用 `chat_ids` 指定一个或多个 Chat。优先使用 `seywo_recall` 读取凝练事项，需要核对事实时再用 `seywo_search`、`seywo_expand` 或 `seywo_read_source` 定位来源。检索是本地、只读和无模型调用的；`seywo_remember` 是唯一的显式工作记忆写入入口，并要求精确来源坐标。

## 显式批量初始化

可一次传入多个明确的项目绝对路径。只处理列出的目录，不递归扫描父目录或寻找其他项目：

```sh
# 只预览，不写入，也不要求确认
seywo/bin/seywo init-batch "/absolute/project-a" "/absolute/project-b" --dry-run

# 先输出文件清单，再输入 yes 确认执行
seywo/bin/seywo init-batch "/absolute/project-a" "/absolute/project-b"
```

如果已经安装 CLI，也可直接使用 `seywo init-batch ...`。预览逐项列出待创建文件、待修改文件和待创建目录，包含初始化和存储锁文件；保留已有自定义文件，因此正常初始化的修改列表为空。原子写入临时文件及 SQLite journal 可能在执行时短暂出现。

只有输入 `yes` 后才会逐个初始化并启用项目 Hook；其他输入、EOF 或确认时按 Ctrl-C 均取消，不写入项目。已有 `.seywo/config.json` 的项目直接跳过，不读取、修复或重置其配置和数据库；重复路径只处理一次。确认后再次检查项目，新增配置会跳过，需要新增未预览文件则报错并要求重新预览。单个项目失败会显示原因并继续其他项目。退出码为 `0`（成功、全部跳过或取消）、`1`（任意项目失败）、`2`（命令参数错误）。初始化不导入历史，也不调用模型。

## 模型和存储

Compact 使用同一套 `maintain → compact_pipeline`。规则先去除工具调用和工具结果，再把用户问题与助手回答按完整轮次送入一次有界模型请求；模型只返回短标题、状态变化、关系操作和来源索引，程序负责校验、合并、版本化和落库。过大的输入按既有边界分批处理。

Codex 是默认模型后端，直接复用宿主的登录和全局模型。模型来源、模型与思考强度通过面板保存到当前 Codex 用户目录的 `seywo/settings.json`，本机所有项目和 Chat 共用。自定义 Responses API 凭据保存在用户目录的 `seywo/credentials/`，不会进入项目仓库。项目运行数据、索引和 Compact 记录留在项目自己的 `.seywo/` 中。升级后重新连接已有 Chat 的 MCP，并在新版面板保存一次模型设置。

Seywo 是独立的新存储和新索引，不兼容或融合 SessionGraph 旧数据。卸载插件不会删除项目数据；删除项目数据前请先按项目需要备份 `.seywo/`。

## 开发和验证

```sh
npm ci --prefix tools/seywo-ui
npm test --prefix tools/seywo-ui
npm run build --prefix tools/seywo-ui
python3 -m pytest -q -c seywo/pyproject.toml seywo/tests
python3 -m compileall -q seywo/src
```

发布包由 `scripts/package-seywo-plugin.py` 生成。Windows、macOS、Linux 共用 `uv` 启动入口，首次运行由其私有缓存准备 Python 和 MCP SDK，Hook 离线复用；Node/npm 只用于开发和构建 UI。完整安装、Hook 信任、模型、存储和验收边界见 [`docs/PLUGIN.md`](docs/PLUGIN.md) 与 [`docs/seywo-mcp.md`](docs/seywo-mcp.md)。
