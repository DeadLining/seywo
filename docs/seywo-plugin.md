# Seywo Codex 插件

当前版本 **0.3.0**。插件自带本地内核、MCP、主线面板、Compact Hook 和 Codex 模型适配器，分发时无需拷贝 SessionGraph/Seywo 独立应用，也不要求另外填写 API Key。

## 安装和首次启用

在 Codex 中添加本仓库为插件市场，安装 `seywo-local` 中的 Seywo。命令行等价操作：

```sh
codex plugin marketplace add /absolute/path/to/repository
codex plugin add seywo@seywo-local
```

安装包入口是 `seywo/.codex-plugin/plugin.json`。当前验收的 Codex 0.152.1 对 Agent Plugins 格式的 Hook 加载有显式跳过分支，因此本包使用其支持 Hook 的 Codex 清单格式。MCP 通过宿主解析出的插件工作目录启动，所有文件随插件迁移，无机器绝对路径，也不追加全局 Hook。

当前支持 macOS/Linux；机器需要 Python 3.11+ 或 uv。运行时优先复用可用的 Python/MCP；有 uv 时由其缓存供给 Python 和固定版本 SDK；只有 Python 时会在私有目录创建环境、安装 `mcp==1.28.1`，不会向用户 Python 中安装包。首次依赖下载需要网络；Node/npm 只用于开发构建。Windows 尚未支持。

正常使用时根据当前 Chat 自动定位项目。首次授权启用项目后创建 `.seywo/.gitignore`、配置和索引，并启用插件整理。初始化和打开面板不调用模型，不扫描其他项目。Codex 的四个插件 Hook 仍需在宿主中审核信任；安装代码不会替用户写入信任。升级后若命令或安装路径变化，宿主可能要求重新审核。已有 Chat 的 MCP 连接可能需要重连。

## 模型与整理

默认后端是 `codex`：通过安装的 `codex exec --ephemeral` 复用 Codex 已有登录、提供方和全局默认模型。它不是直接读取当前 Chat 临时覆盖的模型；设置显式模型时可覆盖。无需 `GRAPH_LLM_API_KEY`，不读取独立应用的模型模板。自定义 Responses API 作为可选来源，在面板设置中保存；密钥存于项目忽略目录的受限凭据文件，状态接口不返回密钥。

整理仍走同一套 `maintain → compact_pipeline`：规则过滤工具调用/工具响应和内部指令；输入多轮用户/AI正文及短历史状态；模型只输出紧凑标题、结构操作与定位索引；程序校验后更新主线、支线、当前重点、合并节点和来源。正常有界批次调用一次；超长输入按既有容量规则分批，不承诺任意长 Chat 仅调用一次。切换到默认 Codex 后端时使用 `seywo-compact-v1`，不修改已有来源和主线内容。

子任务在临时空目录、只读沙箱中运行，关闭 Hook、插件、MCP、工具、网络搜索和记忆发现，结束清理临时文件；不保存新的分析 Chat。当前 CLI 不提供与 Responses 完全等价的输出 token 硬上限，使用 JSON Schema、紧凑输出契约、超时和输出文件大小校验。

真实实测：两轮输入合并为一个节点、一次模型调用，输入 5,085 token / 输出 139 token。原生 Codex 仍有约 4k token 的基础上下文成本，不能将它描述成几十 token 的请求。

## 面板、检索和存储

面板默认展示当前目标、待解决问题、下一步和主线标题，支线折叠、阶段明细和来源按需加载。Compact 是触发事件，没有可视化日记。项目内跨 Chat 检索可指定一个或多个 Chat；原文、索引、历史主线修订和显式记忆能力保留。检索、打开、初始化、保存模型设置均不调用模型；只有用户点击测试才额外调用一次。

Compact 执行表按 Chat 最多保留最近一次成功和最近一次失败，成功清除失败，不重复存储模型 segments/changes。原始安全投影与压缩修订仍用于 MCP 来源回读和固定引用，不承诺删除备份后立即收缩整库。旧独立应用路径只为旧配置保持兼容；插件路径不依赖应用目录。

安装后实际桌面面板和用户真实 Compact 属于宿主验收，须与自动化、隔离项目的模型验收分别记录。
