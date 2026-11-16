# Seywo — See Your Work

**Codex 插件 0.4.0（2026-11-16）**：自带本地内核、MCP、面板和 Compact Hook，默认复用 Codex 登录与全局模型，无需独立应用或额外 Key。macOS/Linux 需要 Python 3.11+ 或 uv，首次可自动安装私有 SDK。安装、宿主信任和验收边界见 [插件说明](docs/PLUGIN.md)。本版按完整轮次提交，当前重点用索引单独维护，模型子进程排除全局 Skills；保留历史引用与 MCP 接口。以下是带日期的历史实现记录。

**MCP 当前入口（2026-11-11）**：已提供独立 stdio MCP，默认检索本项目已导入的全部 chat，可指定一个或多个 chat；支持原文定位、主线读取、来源展开、召回、恢复、覆盖状态和显式工作记忆。检索无模型调用。安装、工具契约和回退方法见 [Seywo MCP](../docs/seywo-mcp.md)。以下带日期的核验段落是历史记录。

**长 chat 修复进展（2026-11-08）**：连续来源窗口、消息内定位、共享预算、B 稀疏历史与分批整理已实现，指定完整原始段已完成隔离手动回归，整体 V3 与用户验收仍未全部完成。[当前记录](../docs/seywo-long-evidence-20261108/progress.md)。以下固定回归结果属于此前基线，不能代替本轮当前源码的完整验收。


**当前核验（2026-11-08）**：`seywo-index-3` 已修复已定位的语义缺陷，[14例固定回归与核验报告](../docs/seywo-semantic-verification-20261108.md)经 agent 对照来源通过；218 项工程、29 项旧回归、ruff、离线包18步通过。连续 v1–v4 保留人工锁与迟到反证。扩大标注长 chat 的质量/成本稳定性与指定完整真实 chat 的用户验收仍未完成，历史广义失败门禁不提升。S4/S5 未实施，不接 Hook、不安装。

看清你的工作。节点以短标题展示，详情和原文点击展开；当前协议为 `seywo-index-3`。S1 提供独立的项目初始化、Codex 原始记录导入、安全投影、来源回读与全文搜索。源搜索结果都是 `source_only`；S2 已增加有界理解请求/候选校验流水线，S3 另提供主支线整理请求、版本化阅读页和人工修正；当前已接入 compact Hook，并另行提供独立 MCP 服务；各阶段的历史核验范围见下文。

这是独立 Python 包（Python 3.11+、SQLite FTS5），不导入 SessionGraph，不修改其安装注册或数据。安装入口由本包的 `pyproject.toml` 定义为 `seywo`；开发时可直接运行：

```sh
PYTHONPATH=/path/to/sessiongraph/seywo/src python3 -m seywo init /path/to/project
PYTHONPATH=/path/to/sessiongraph/seywo/src python3 -m seywo \
  --config /path/to/project/.seywo/config.json \
  import /path/to/selected-rollout.jsonl --chat HOST_CHAT_ID --order 0
PYTHONPATH=/path/to/sessiongraph/seywo/src python3 -m seywo \
  --config /path/to/project/.seywo/config.json search '工作脉络'
PYTHONPATH=/path/to/sessiongraph/seywo/src python3 -m seywo \
  --config /path/to/project/.seywo/config.json events --chat HOST_CHAT_ID
PYTHONPATH=/path/to/sessiongraph/seywo/src python3 -m seywo \
  --config /path/to/project/.seywo/config.json read EVENT_ID --start 0 --end 20
```

`init` 先写 `.seywo/.gitignore`（`*` 与 `!.gitignore`），再创建配置和新库；重复执行保留项目 ID 和用户规则。命令默认只看当前目录的 `.seywo/config.json`，可显式指定配置；不会向上查找旧配置或扫描会话目录。

每次导入要求明确指定原始文件、chat 和非负分段顺序。JSONL 第一条 `session_meta.payload.id` 必须匹配 chat；提供 cwd 时必须匹配项目根目录。新文件占用已有分段顺序、改变原有绑定、混入多个会话身份都会失败。fork 必须以它自己的 chat ID 绑定。没有分段顺序自动推断，也没有相似标题合并。

来源的稳定 ID 绑定物理分段及记录号，快照包含源文件哈希与投影版本。追加/改写后发布新的不可变快照，保留旧版；没有宿主稳定事件 ID 的记录若在文件中间插入或重排，只能按新快照的定位解释，不承诺跨重排的语义同一性。`read --snapshot ID` 可回读历史投影；历史工具配对仅在该物理快照内查询，当前配对在 chat 当前分段集合中查询。缺失和歧义都明确返回。

源文件只按明确路径读取，不递归导入工具提到的文件。保存每条记录的原始字节区间/哈希、投影文本的 Unicode 字符区间/哈希、正文 JSON 字段映射、归属、调用 ID 和覆盖状态。只持久化安全投影，不复制未脱敏的原始文件。当前脱敏覆盖已知凭据模式、私钥与 base64 data URI，不能保证识别任意秘密；未知媒体、未知事件、宿主封装、坏 JSON/UTF-8 和末尾未结束行保留为待处理缺口。内部推理、system/developer 指令和已知镜像流不进入搜索。工具正文中的任意指令仅作为数据保存，不执行。

一次导入在事务内发布，源文件读取期间变化、单行超过 16 MiB、解析适配失败或数据库异常会回滚，保留此前快照；超大行报错，不截断后冒充成功。重复导入相同内容复用快照。搜索使用独立 FTS5 和版本化中文单字/双字及标识符切分，搜索 token 与模型 token 无关。`reindex` 只从 Seywo 已保存的当前安全投影重建，源文件丢失也能回读/重建。

`delete-chat --chat ID` 删除 Seywo 中该 chat 的所有快照、正文、覆盖和搜索记录，留下删除标记防止在途/重试导入复活；不会删除原始文件，也不等于清除外部备份或文件系统历史。S1 未提供撤销删除入口。

`provider.py` 冻结单次请求容量与计量契约：输入上限同时受应用、提供方输入和上下文减输出预留/协议余量约束。未接入 tokenizer 时，UTF-8 字节数加余量只作为保守预算估计；实际提供方协议隐藏开销、tokenizer/usage 和容量仍待验证。整个请求（含提示、Schema 等）参与计量，超限返回 `split_or_wait`，不静默截正文。没有网络请求或真实模型调用。

验证：

```sh
python3 -m pytest -q -c seywo/pyproject.toml seywo/tests
python3 -m ruff check seywo/src seywo/tests
```

完整方案见 [Seywo V3](../docs/seywo-v3-design.md)，阶段契约见 [S1 契约](../docs/seywo-s1-contract.md)。

## S2 有界理解与证据候选

新增 `understand` 请求 outbox。程序分片并持久化请求，调用方显式对接模型后提交结果；也可注入 `FunctionBackend` 或 `JevBackend` 的 transport。没有自动发现凭据、隐式 API 请求或旧后端回退。

示例 options JSON（容量只是调用方明确填写的预算，不表示实测提供方能力）：

```json
{
  "backend": "your-provider-adapter",
  "model": "your-explicit-model",
  "capacity": {"context_limit": 32000, "input_limit": 28000, "output_limit": 4000},
  "application_cap": 24000,
  "output_reserve": 4000
}
```

在已完成初始化与显式导入的临时项目中，使用相同 `--config`：

```sh
seywo --config /path/to/project/.seywo/config.json understand prepare --chat HOST_CHAT_ID --options options.json
seywo --config /path/to/project/.seywo/config.json understand claim JOB_ID --backend your-provider-adapter
seywo --config /path/to/project/.seywo/config.json understand submit ATTEMPT_ID --backend your-provider-adapter --response response.json
seywo --config /path/to/project/.seywo/config.json understand show JOB_ID
```

`claim` 返回的 request 包含规则、程序预分配的来源/事项编号和精简输出契约；原文定位/长 ID/偏移/版本仅保留在本地私有映射。工具返回和调用参数不进入所有模型路径，来源仍供人查看。response 文件包含 `output`、可选 `usage`（input_tokens/output_tokens）和 `finish`（complete/length/error）。准备和提交不直接发网络请求；`show` 不触发模型。需要处理所有 extract、readback 和 merge 请求后才有最终候选。

结果保留 candidate_only、依据、来源角色、反证关系与 unresolved，不代表用户确认、主线发布或任务完成。合并和回读超限保留 waiting_context；`understand context JOB_ID --options larger-options.json` 显式增加输入容量且保留累计账本。`understand budget JOB_ID --limits larger-limits.json` 只增加限额。具体协议/恢复与模型评估边界见 [S2 契约](../docs/seywo-s2-contract.md)。

S2 工程测试使用自造材料和受控后端。真实通用模型/Jev 另以保存设置手动验证合成材料；长上下文稳健性、实际 tokenizer/最大容量和指定真实 chat 语义仍未验收。

## S3 单 chat 阅读与人工修正

已有 S2 最终候选后，准备 B 整理请求，仍由同一 outbox 显式接模型；提交后另行发布：

```sh
seywo --config /path/to/project/.seywo/config.json narrative prepare JOB_ID
seywo --config /path/to/project/.seywo/config.json understand claim JOB_ID --backend your-provider-adapter
seywo --config /path/to/project/.seywo/config.json understand submit ATTEMPT_ID --backend your-provider-adapter --response organize-response.json
seywo --config /path/to/project/.seywo/config.json narrative publish TASK_ID
seywo --config /path/to/project/.seywo/config.json narrative show --chat HOST_CHAT_ID
seywo --config /path/to/project/.seywo/config.json narrative serve --chat HOST_CHAT_ID
```

`serve` 输出本机 loopback URL，Ctrl-C 退出。页面能读主线/支线/停点、展开证据与相邻原文，修正文案、位置、焦点与支线状态，拆分事项、合并阶段、补充人工事项及处理待审修订。每次保存产生新版本；历史版本只读，源变动显示待复核。CLI 也可用 `narrative edit --chat ID --amendment FILE` 提交 actor/base_version/override_revision/patch。

主线组织仍是未审核的模型候选；返回主线不等于支线解决，人工改字不等于批准执行。受保护人工内容不会被模型静默覆盖。整理请求占同一累计账本，输入应用上限 16000；过大时等待，不丢弃候选。当前已实现工程阅读闭环和精简增量 B；已应用过的同一 A 候选批次在人工修改后不会再次提交。真实模型只验证合成材料，指定真实 chat 的语义/人工验收仍未运行。详见 [S3 契约](../docs/seywo-s3-contract.md)。

2026-11-06 按“先验证，不接入 Hook”完成工程复核：96 项 Seywo 测试、29 项旧能力回归、lint 和离线包内 CLI 通过；修复错误 JSON 数值类型可绕过版本/来源范围校验的问题。真实模型/Jev 凭据缺失，语义验收仍未运行。见 [验证记录](../docs/seywo-s1-s3-verification-20261106.md)。

同日已按用户指定设置执行真实 `deepseek-v4-flash-0731` 验证，不依赖环境变量密钥。前轮旧协议四组样例两组发布、两组中止，另有主任务误放支线的问题；修正协议/跨角色证据校验后工程测试100项通过，真实语义验收仍未完成。见 [模型首轮实测](../docs/seywo-model-evaluation-20261106.md)。手动评估入口为 `evals/model_probe.py`，不接入 Hook。


2026-11-06 前轮精简协议实现 `seywo-index-1`（历史）。模型只选 index、类型/关系和变化，程序补全原文引用与事务信息。合并默认保留旧事项/未决，B 默认保留既有阅读状态；显式解决需依据，旧请求不能重新发送并且历史消耗不重置。数据库 schema4 与 `codex-safe-v2` 投影只在独立 Seywo 内使用，`.seywo/.gitignore` 默认规则继续生效。当前 146 项 Seywo 工程验证、29 项既有能力回归、lint、离线 wheel 与隔离包内 CLI 通过。当前真实模型结果与限制见 [本轮优化验证](../docs/seywo-index-verification-20261106.md)，早期验证记录仍为历史结果。

前轮精简协议工程验证已通过，但复杂主支线实测仍漏掉最后一次返回主线，当前焦点错误停在支线；语义门禁保持 failed。当前代码验证区分原始真实调用与请求完全相同的响应回放，详见本轮优化验证。

2026-11-06 短标题更新：模型为每个新事项提炼 `title`，建议中文6–15字、硬上限40字符；必要说明放 `detail`，程序回填原文到本地详情与证据。阅读页、目标/下一步和依赖使用标题，详情默认折叠；人工改标题、拆分和新增保存标题并受版本/锁保护。旧无标题节点显示“历史事项（待补标题）”，不改历史。163项Seywo、29项旧回归、离线wheel/18步CLI通过，2次保存设置的真实模型调用及浏览器读改历史验证通过；仅短标题样例，不代表复杂主支线语义已修复。[验证记录](../docs/seywo-title-verification-20261106.md)。

连续长 chat 的显式窗口、共享预算、稀疏历史、依赖分批与 compact 镜像规则见 [当前 S2 契约](../docs/seywo-s2-contract.md)。

## compact hook（2026-11-09 已接入）

当前项目的 `~/.codex/hooks.json` 已切换为 Seywo：保留 PromLight，移除 SessionGraph Hook，并由 `seywo/bin/seywo-hook` 接管 `PreCompact`、`PostCompact`、`SessionStart` 和 `UserPromptSubmit`。Hook 只做三件事：

1. 解析 payload，向上找到项目 `.seywo/config.json`（未初始化或 `hook.enabled=false` 时直接返回）；
2. PreCompact / PostCompact 将原始字节保存为共享压缩块与内容寻址的不可变清单，然后写一条持久 inbox 记录；
3. SessionStart / UserPromptSubmit 从 Seywo 当前主线读取有界恢复上下文，只输出 `additionalContext`，不调模型、不写数据库。

边界 Hook 本身不建库、不调模型；PreCompact / PostCompact 以 detached maintainer 方式继续处理，进程恒定退出 0。

后台 maintainer 在所有入口共用单 chat 锁，领取 inbox 后统一校验归档并导入。
当前项目配置 `seywo-compact-v1`：程序先按原生 turn 组成用户问题与助手最终答复的多轮片段，规则层删除 tool call、tool response 和机械确认；模型一次处理一个 Compact 批次，只返回少量片段、短标题、操作和 turn index。程序负责证据坐标、节点 ID、主线/支线、焦点和 SQLite 原子发布。
正常 Compact 只有一次模型调用；超过字符或轮次预算时才按连续 turn 分批。分批期间下一批读取前一批的内存状态，全部成功后一次性推进 cursor；失败不半发布，可重试。
`compact_batches` 保存调用、token、片段和变更收据，原始消息仍只在 source store 中保存并可定位。旧 `seywo-delta-v1` 与 Understanding/A-B 流程继续保留，作为兼容入口。

```sh
seywo compact enable            # 打开本项目的 hook（--disable 关闭，0 次模型调用）
seywo compact status            # 待处理/失败记录，0 次模型调用
seywo compact run --chat ID     # 手动跑一次（与 hook 共用游标和账本）
seywo compact retry --chat ID   # 把失败记录放回队列
```

跨 compact 仅在完整字节前缀验证通过时复用分段；文件重写创建后续分段。
已知旧快照重投不会改写当前 head。复制前缀按原生轮次／时间与原始记录身份识别，文字相同的独立消息仍保留证据。
归档发布与回收共用锁；清理必须先验证全部引用。`storage verify` 和 `storage migrate --dry-run` 不创建文件、不执行清理。
已有数据的 schema 升级必须通过 `seywo storage migrate`，保留 WAL 安全备份和迁移日志；清理需显式执行 `seywo storage gc`。
