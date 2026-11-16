# Seywo 0.4.0 Compact 逻辑重构验收（2026-11-16）

本次重构已实现、回归通过并安装至 Codex 插件缓存。最终包与源码、已安装运行文件逐字节一致。最新桌面后端检查中，4 个 Hooks 均为 trusted。当前桌面 chat 的下一次真实自动 Compact 尚未执行；不能将显式内核验证或隔离流程测试当作该自动路径的实测结果。

## 逻辑变更

1. **以完整原生问答为提交单位。** 来源采集与语义提交分离；规则保留 task_started/task_complete/turn_aborted 元数据，选择已闭合的对话前缀。尚未结束的尾轮延后，不调用模型、不推进语义游标。旧游标落在轮次中间时，恢复该轮原始问题和回答；最小轮次进度表与主线修订、游标原子提交。重复 Hook 和已提交回答之后的结束元数据不会重复调用模型。
2. **当前主线与历史事项分离。** 模型在原有抽取调用中返回 current 索引，最多选择 1 个目标、3 个问题、3 个下一步；运行时验证引用、类型和状态后生成当前概览。历史未完成事项保留来源和可检索性，退出当前概览无需伪造为完成。支持仅当前选择变化时发布修订，尊重手工主线、标题及内容锁。
3. **缩小模型任务和固定上下文。** 输入按对话规则筛选，工具调用及响应正文不进入语义抽取。历史目录在 12 项预算内保留当前目标，并按相关性、当前阶段和新近程度选择。模型输出短标题、操作及来源索引；JSON Schema 只提交一份。抽取子进程隔离用户技能目录、插件、MCP、Hook、记忆等无关上下文，不修改用户模型配置。
4. **审计模型选择与成本。** 每次调用冻结配置选定的模型、provider，收据记录调用次数和输入、输出、缓存 tokens。该记录证明 Codex 选择的目标；不能证明网关私下转发的真实上游。收据仍按既有数量上限保留，不存完整提示词或模型原始输出。独立应用和插件共用内核，保留独立 Responses 后端。

## 验证结果

| 验证项 | 结果 | 证据 |
| --- | --- | --- |
| Python 完整回归 | 556 通过；1 条现有 dateutil 弃用警告 | [python-tests.log](seywo-compact-redesign-20261116/python-tests.log) |
| 插件界面回归 | 16 通过 | [ui-tests.log](seywo-compact-redesign-20261116/ui-tests.log) |
| 静态检查 | Ruff、compileall、git diff --check 通过 | [final-checks.json](seywo-compact-redesign-20261116/final-checks.json) |
| 独立只读审查 | 发现的 3 个 P2 已修复并补回归：未写完的 JSONL、旧游标后的中断轮、标题更新覆盖内容锁 | [review.md](seywo-compact-redesign-20261116/review.md) |
| 隔离 Hook 流程 | PreCompact/PostCompact 完成，1 次抽取；确定性本地 provider，无语义质量证明 | [lifecycle.json](seywo-compact-redesign-20261116/lifecycle.json) |
| 已安装桌面协议检查 | 0.4.0、16 个 MCP 工具、当前 chat 自动绑定、来源和主线读取通过；0 模型调用 | [installed-desktop.json](seywo-compact-redesign-20261116/installed-desktop.json) |
| 桌面 Hook 状态 | 4 项 enabled、trusted；本验证未写入信任配置 | [hooks-after.json](seywo-compact-redesign-20261116/hooks-after.json) |
| 包与安装一致性 | 73 个插件文件与源码、安装目录一致，ZIP 内容一致 | [artifact-consistency.json](seywo-compact-redesign-20261116/artifact-consistency.json) |

测试覆盖未结束轮次零调用、完整轮次成对抽取、重复触发零调用、失败重试、旧游标恢复、非法 current 引用拒绝、手工锁、来源坐标与历史正文复用。保留既有 MCP、搜索、来源回读和人工修改契约；这些结果是已覆盖场景的回归证据，不等于所有场景都不会出现问题。

## 真实模型与 token

相同短对话、相同本机 Codex 模型配置，对比旧 0.3.0 与新 0.4.0 抽取协议和传输路径：

| 指标 | 旧路径 | 新路径 |
| --- | ---: | ---: |
| 输入 tokens | 5,143 | 2,487 |
| 输出 tokens | 168 | 199 |
| 合计 tokens | 5,311 | 2,686 |
| 耗时（秒） | 18.891 | 31.315 |

输入减少 **51.64%**，合计减少约 **49.4%**。这是单组真实样本，不是长期平均收益；本次新路径耗时更长，没有速度提升结论。新路径记录目标模型 gpt-6.1-sol、provider crs。另一个真实语义用例验证了“Hook 已就绪，但依赖尚未验证”的复合任务仍保持未完成。

详见 [real-model-final.json](seywo-compact-redesign-20261116/real-model-final.json)。模型仍可能保留已解决事项的旧标题措辞；状态与当前选择通过验证，不据此声称标题质量已穷尽验收。

另用本地确定性 provider 捕获原生请求：技能目录移除后，该夹具输入由 7,213 字符降至 2,066 字符。此数据为夹具输入，不是实际 token 账单。原生请求仍保留小型 request_user_input 工具定义，不能声称宿主固定开销已全部消失。见 [native-context.json](seywo-compact-redesign-20261116/native-context.json)。

## 本项目实跑

通过**已安装内核显式执行维护**，处理上一轮已完成问答，修订 76 → 77：1 次模型调用，3,789 输入 / 471 输出 tokens，更新 5 个已有事项，新增、合并、删除、隐藏均为 0；324 个历史事项全部保留。当前概览收敛为 1 个目标、1 个问题、2 个下一步。

该轮的用户问题和助手回答均有来源证据；本次正在进行的修复轮保持 pending，尚未提交。因此版本 77 中的待办措辞反映此前已完成问答，不代表本次修复的最新验收状态。详见 [current-project-run.json](seywo-compact-redesign-20261116/current-project-run.json)、[current-project-result.json](seywo-compact-redesign-20261116/current-project-result.json)、[current-project-coverage.json](seywo-compact-redesign-20261116/current-project-coverage.json)。

## 交付及剩余验证

- 版本：0.4.0。
- 安装位置：`/Users/shengzhoukong/.codex/plugins/cache/seywo-local/seywo/0.4.0`。
- 最终安装包：`dist/seywo-release-20261116/seywo-plugin-0.4.0.zip`；SHA-256：`b809dc31e1711f359b9523942c1f568fad019946cf5598aea4288f30d826fd16`。
- 桌面后端：0.169.0-alpha.11.1；4 个 Hooks 最新状态均 trusted，无需再次审核。
- 剩余：用户在当前桌面 chat 执行下一次 Compact，确认新版自动触发结果。隔离 Hook 测试用 CLI 0.152.1，通过不替代该桌面实测。此次没有重新进行原生面板的视觉验收。
