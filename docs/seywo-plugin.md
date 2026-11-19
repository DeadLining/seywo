# Seywo Codex 插件

当前版本 **0.4.0**。插件自带本地内核、MCP、主线面板、Compact Hook 和 Codex 模型适配器，分发时无需拷贝 SessionGraph/Seywo 独立应用，也不要求另外填写 API Key。

## 安装和首次启用

在 Codex 中添加本仓库为插件市场，安装 `seywo-local` 中的 Seywo。命令行等价操作：

```sh
codex plugin marketplace add /absolute/path/to/repository
codex plugin add seywo@seywo-local
```

安装包入口是 `seywo/.codex-plugin/plugin.json`。当前验收的 Codex 0.152.1 对 Agent Plugins 格式的 Hook 加载有显式跳过分支，因此本包使用其支持 Hook 的 Codex 清单格式。MCP 通过宿主解析出的插件工作目录启动，所有文件随插件迁移，无机器绝对路径，也不追加全局 Hook。

Windows、macOS、Linux 使用同一个插件和同一个 `uv` 启动入口。机器需先安装 `uv`，并确保 Codex 能在 PATH 中找到它；`uv` 在私有缓存中准备 Python 3.11+ 与固定的 `mcp==1.28.1`，不会修改系统 Python。首次 MCP 连接允许下载依赖；Hook 使用相同缓存并强制离线，不在 compact 回调里下载或安装。没有 `uv` 时需要先完成这一项运行依赖安装，无需手动选择操作系统、Python 路径或 Shell。Node/npm 只用于开发构建。

平台差异集中在 `runtime_platform.py` 与按需加载的 `win32_native.py`：Unix 使用 flock/进程组；Windows 使用 LockFileEx/Job Object，模型进程先挂起、加入 Job 后恢复，防止超时留下子进程。已有数据库、索引、主线和 MCP 工具契约无需迁移。可选 Responses API 的 Windows 凭据通过当前用户 DPAPI 加密，不依赖 Unix 权限位。Windows 工作区 URI、npm Codex shim 和 SQLite 路径由适配层处理。

原生验收矩阵位于 `.github/workflows/portable-runtime.yml`，在 Windows、macOS、Linux 运行完整 Python 测试、真实声明入口/离线 Hook 检查、UI 测试和打包检查。本地 macOS 测试不能替代 Windows 原生执行结果。

正常使用时根据当前 Chat 自动定位项目。首次授权启用项目后创建 `.seywo/.gitignore`、配置和索引，并启用插件整理。初始化和打开面板不调用模型，不扫描其他项目。Codex 的四个插件 Hook 仍需在宿主中审核信任；安装代码不会替用户写入信任。升级后若命令或安装路径变化，宿主可能要求重新审核。已有 Chat 的 MCP 连接可能需要重连。

## 模型与整理

运行时优先使用宿主提供的 Codex 可执行文件，避免误用 PATH 中另一套旧 CLI；缺少宿主路径时才使用 PATH 或已知应用位置。

默认后端是 `codex`：通过安装的 `codex exec --ephemeral` 复用 Codex 已有登录、提供方和全局默认模型。它不是直接读取当前 Chat 临时覆盖的模型；设置显式模型时可覆盖。无需 `GRAPH_LLM_API_KEY`，不读取独立应用的模型模板。自定义 Responses API 作为可选来源，在面板设置中保存；密钥存于项目忽略目录的受限凭据文件，状态接口不返回密钥。

模型设置提供“跟随 Codex 默认模型”和当前 Codex 模型列表。列表通过原生 `model/list` 读取，不创建分析 Chat、不生成模型内容；可选模型的账户权限和提供方兼容性仍由显式测试确认。所选模型可测试并保存到项目配置。运行时只从该模型声明支持的推理强度中选择较低强度，不对所有模型固定传入 `low`。列表失败保留原选择，并提供重新读取入口。

测试失败显示固定的可操作原因，区分登录、模型权限、CLI 接口不兼容、推理配置、限流、网络与超时；不展示原始 stderr、提供方响应或密钥。

整理仍走同一套 `maintain → compact_pipeline`：规则过滤工具调用/工具响应和内部指令；输入多轮用户/AI正文及短历史状态；模型只输出紧凑标题、结构操作与定位索引；程序校验后更新主线、支线、当前重点、合并节点和来源。正常有界批次调用一次；超长输入按既有容量规则分批，不承诺任意长 Chat 仅调用一次。切换到默认 Codex 后端时使用 `seywo-compact-v1`，不修改已有来源和主线内容。

子任务在临时空目录、只读沙箱中运行，关闭 Hook、插件、MCP、工具、网络搜索和记忆发现，结束清理临时文件；不保存新的分析 Chat。当前 CLI 不提供与 Responses 完全等价的输出 token 硬上限，使用 JSON Schema、紧凑输出契约、超时和输出文件大小校验。

真实实测：两轮输入合并为一个节点、一次模型调用，输入 5,085 token / 输出 139 token。原生 Codex 仍有约 4k token 的基础上下文成本，不能将它描述成几十 token 的请求。

## 面板、检索和存储

面板默认展示当前目标、待解决问题、下一步和主线标题，支线折叠、阶段明细和来源按需加载。Compact 是触发事件，没有可视化日记。项目内跨 Chat 检索可指定一个或多个 Chat；原文、索引、历史主线修订和显式记忆能力保留。检索、打开、初始化、保存模型设置均不调用模型；只有用户点击测试才额外调用一次。

Compact 执行表按 Chat 最多保留最近一次成功和最近一次失败，成功清除失败，不重复存储模型 segments/changes。原始安全投影与压缩修订仍用于 MCP 来源回读和固定引用，不承诺删除备份后立即收缩整库。旧独立应用路径只为旧配置保持兼容；插件路径不依赖应用目录。

安装后实际桌面面板和用户真实 Compact 属于宿主验收，须与自动化、隔离项目的模型验收分别记录。

模型设置中的“思考强度”随当前模型更新可选档位，默认“自动（优先低消耗）”。手选档位同时用于测试模型和后续 Compact；保存自动会清除强度覆盖。模型目录读取和强度选择不调用模型生成，手选档位无法确认支持时会在生成前提示。
