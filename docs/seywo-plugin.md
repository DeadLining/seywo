# Seywo Codex 插件

当前版本 **0.5.5**。插件自带本地内核、MCP、主线面板、Compact Hook 和 Codex 模型适配器，无需独立应用，也不要求另外填写 API Key。版本变更见[更新日志](../CHANGELOG.md)。

## 安装和首次启用

在 Codex 中添加本仓库为插件市场，安装 `seywo-local` 中的 Seywo。命令行等价操作：

```sh
codex plugin marketplace add /absolute/path/to/repository
codex plugin add seywo@seywo-local
```

安装包入口是 `seywo/.codex-plugin/plugin.json`。当前验收的 Codex 0.152.1 对 Agent Plugins 格式的 Hook 加载有显式跳过分支，因此本包使用其支持 Hook 的 Codex 清单格式。MCP 通过宿主解析出的插件工作目录启动，所有文件随插件迁移，无机器绝对路径，也不追加全局 Hook。

Windows、macOS、Linux 使用同一个插件和同一个 `uv` 启动入口。机器需先安装 `uv`，并确保 Codex 能在 PATH 中找到它；`uv` 在私有缓存中准备 Python 3.11+ 与固定的 `mcp==1.28.1`，不会修改系统 Python。首次 MCP 连接允许下载依赖；Hook 使用相同缓存并强制离线，不在 compact 回调里下载或安装。没有 `uv` 时需要先完成这一项运行依赖安装，无需手动选择操作系统、Python 路径或 Shell。Node/npm 只用于开发构建。

平台差异集中在 `runtime_platform.py` 与按需加载的 `win32_native.py`：Unix 使用 flock/进程组；Windows 使用 LockFileEx/Job Object，模型进程先挂起、加入 Job 后恢复，防止超时留下子进程。平台适配不改变主线和 MCP 工具契约。可选 Responses API 的 Windows 凭据通过当前用户 DPAPI 加密，不依赖 Unix 权限位。Windows 工作区 URI、npm Codex shim 和 SQLite 路径由适配层处理。

原生验收矩阵位于 `.github/workflows/portable-runtime.yml`，在 Windows、macOS、Linux 运行完整 Python 测试、真实声明入口/离线 Hook 检查、UI 测试和打包检查。本地 macOS 测试不能替代 Windows 原生执行结果。

正常使用时根据当前 Chat 自动定位项目。首次授权启用项目后创建 `.seywo/.gitignore`、配置和索引，并启用插件整理。初始化和打开面板不调用模型，不扫描其他项目。Codex 的五个插件 Hook 仍需在宿主中审核信任；安装代码不会替用户写入信任。升级后若命令或安装路径变化，宿主可能要求重新审核。已有 Chat 的 MCP 连接可能需要重连。

## 模型与整理

运行时优先使用宿主提供的 Codex 可执行文件，避免误用 PATH 中另一套旧 CLI；缺少宿主路径时才使用 PATH 或已知应用位置。

默认后端 `codex` 使用宿主的 **app-server JSON-RPC**：模型目录、账号状态、测试和压缩使用同一个运行时接口。每个拥有它的插件 MCP 或后台整理进程复用自己的 Worker；每次生成创建独立的临时 Thread，结束后释放，不继承上一次语义上下文，也不生成用户可见的分析 Chat。后台整理是独立进程，不与 MCP 跨进程共享 Worker。

Seywo 的模型来源、模型选择和思考强度属于当前用户，在本机所有项目和 Chat 之间统一生效；项目只保存对话索引、主线和 Hook，不再覆盖模型选择。不会读取当前 Chat 临时选择的模型，也不直接调用当前 App Chat 的模型会话。不再调用 `codex exec` 或 `codex login status`，不读取 App 私有账号令牌。仍需要宿主提供的 Codex 可执行文件来运行 `app-server --stdio`，无需独立 Seywo 应用或额外 API Key。

账号由 app-server 管理。缺少账号授权时，面板显示“连接 Codex 账号”，通过官方浏览器授权，再点击“检查授权”并测试。授权检查和模型目录读取不生成模型内容。若 App 的现有授权未向应用服务开放，仍需完成这次授权，不能保证 App 登录态天然共享。可选 Responses API 后端也按用户级设置保存，密钥存于当前用户 Seywo 目录的受限凭据文件，不进入项目仓库。

模型列表通过 `model/list` 分页读取；生成复用最多 60 秒的目录缓存，显式刷新绕过缓存，Codex 配置变化重建 Worker。思考强度仅从目标模型声明支持的档位选择；手选档位无法确认支持时，在生成前返回原因。保存设置、检索、打开、初始化和账号操作均不调用模型；用户点击测试才额外生成一次。

整理仍走 `maintain → compact_pipeline`：规则移除工具调用、响应和内部指令；模型接收用户/AI正文及短历史状态，输出紧凑标题、结构操作与定位索引。Schema 随 `turn/start.outputSchema` 传入一次，不写临时 Schema 或输出文件。结构、语义校验及有界 repair 继续由 compact 管线负责；失败可重试，原有来源、版本和发布幂等性保持既有规则。超长输入仍按容量分批，不能承诺任意长度只调用一次。

Worker 在临时空目录、只读沙箱中运行，关闭 Hook、插件、MCP、全局技能发现、记忆、Shell、图片、目标管理及搜索入口。宿主仍可能附带 `request_user_input` 定义，较旧版本还会附带 `apply_patch`，任何工具调用都会拒绝本次结果。超时中断并清理进程树；协议损坏后下一次请求重建 Worker，运行时不暗中重放生成请求。用户可见错误为固定原因，区分授权、协议兼容、模型权限、限流、网络、中断和超时，不返回原始 stderr 或凭据。

app-server 没有与 Responses 完全等价的输出 Token 硬上限；Seywo 使用紧凑 Schema、领域校验、超时、单条消息/累计字节上限。当前机器两次真实连通测试使用 `crs / gpt-6.1-sol / low`，输入均为 **5,728 token**，输出分别 **15 / 29 token**，耗时 **4.832 / 9.181 秒**。这是该提供方和宿主版本下的探测成本，不能等同于实际 compact 或账号模式结果。本地假提供方另行验证请求中没有全局技能目录且不持久化 Thread；字符数不等同于模型 Token 数。

验证命令：`pytest -q seywo/tests`、`node --test seywo/ui/panel.test.mjs`。安装 Codex 的主机可额外运行 `python3 scripts/verify-seywo-app-server.py`，检查真实宿主协议与本地假提供方的隔离契约，不消耗真实模型额度。浏览器 OAuth 全流程与原生 Windows/macOS/Linux 发布验收需分别记录。

## 面板、检索和存储

面板默认展示当前目标、待解决问题、下一步和主线标题，支线折叠、阶段明细和来源按需加载。Compact 是触发事件，没有可视化日记。项目内跨 Chat 检索可指定一个或多个 Chat；原文、索引、历史主线修订和显式记忆能力保留。检索、打开、初始化、保存模型设置均不调用模型；只有用户点击测试才额外调用一次。

Compact 执行表按 Chat 最多保留最近一次成功和最近一次失败，成功清除失败，不重复存储模型 segments/changes。安全投影与压缩修订仍用于 MCP 来源回读和固定引用。

安装后实际桌面面板和用户真实 Compact 属于宿主验收，须与自动化、隔离项目的模型验收分别记录。

模型设置中的“思考强度”随当前模型更新可选档位，默认“自动（优先低消耗）”。手选档位同时用于测试模型和后续 Compact；保存自动会清除强度覆盖。模型目录读取和强度选择不调用模型生成，手选档位无法确认支持时会在生成前提示。

## 存储生命周期

来源出现位置和引用 ID 保持稳定，相同描述与任务正文使用共享压缩存储；历史任务仍可逐字回读。全文索引保留用户、AI 和工具内容的原有检索语义，从现有压缩正文派生检索文本。原始归档按内容分块，无损复用多次 compact 中重复的内部历史。

成功发布或明确无变化后，统一回收没有数据库或失败重试引用的临时来源、孤立正文和孤立块；失败任务所需的来源保留。活跃写入存在时推迟回收。只有实际空闲页达到阈值才收缩数据库，不在每次 compact 执行全库 VACUUM。回收不调用模型，失败只提示存储维护状态，不撤销已发布主线，下次成功整理后重试。

新项目直接创建 schema 8 索引，现有 schema 8 项目无需迁移。其他格式会被拒绝，原库不修改；如需从空索引开始，先移出旧 `.seywo` 状态目录。新版不生成整库回滚备份。校验和显式回收命令：

```sh
seywo/bin/seywo --config "$PWD/.seywo/config.json" storage verify
seywo/bin/seywo --config "$PWD/.seywo/config.json" storage gc
```

Compact 成功后可回收已入库的精简快照，检索历史仍读数据库。当前索引仍引用的归档保留。

已启用 Hook 的项目在会话开始、结束及 Compact 后核对 Codex Desktop 的删除标记和后端目录。两者都证实永久删除时，只清理该项目中对应 Chat 的 Seywo 数据，并阻止旧任务重新导入。归档、关闭会话或日志缺失不等于删除。关闭的 Chat 如未触发 Hook，在该项目下次活动时补清理；不设常驻轮询。Seywo 不修改 Codex 原始对话或宿主数据库。


## 用户级模型设置

模型来源、模型和思考强度保存到当前 Codex 用户目录的 `seywo/settings.json`，所有项目共享。自定义 API 凭据保存到同目录的 `credentials/`。项目配置不再覆盖模型选择，模型设置不需要项目初始化。

从旧版升级时，先更新插件安装，再重新连接已有 Chat 的 MCP；已加载的旧进程不会因 Git 拉取自动变成新代码。旧版项目里的选择不自动迁移：在新版面板保存一次即可统一生效。新版设置面板显示“适用于本机所有项目和对话”。已打开的模型设置关闭后重新打开，会读取最新保存值。

安装验收执行 `python3 scripts/verify-installed-seywo-plugin.py --project /absolute/project`，逐文件核对运行时、面板与入口和当前源码一致，避免仅凭版本号通过验收。该检查不调用模型。
