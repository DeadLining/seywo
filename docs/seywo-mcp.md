# Seywo MCP

Seywo 的 MCP 是 Codex 使用历史记录的入口。插件模式会根据当前 Chat 的宿主上下文自动绑定当前项目；独立运行时可以显式传入项目配置。两种入口使用同一个本地内核和同一套九个历史工具，读取不会调用模型、访问网络或自动导入内容。

## 工具

| 工具 | 用途 |
| --- | --- |
| `seywo_search` | 检索当前项目已导入的用户/助手对话；省略 `chat_ids` 搜索全项目，也可指定一个或多个 Chat。 |
| `seywo_read_source` | 通过 `chat_id/event_id/snapshot_id` 校验来源后，按字符范围读取安全原文。 |
| `seywo_read_narrative` | 读取某个 Chat 的主线、阶段、支线和分页节点。 |
| `seywo_expand` | 展开搜索命中的 opaque ID，回读节点、记忆或来源证据。 |
| `seywo_packet` | 在一个 UTF-8 字节预算内组合检索命中和有限原文片段。 |
| `seywo_recall` | 优先读取当前主线事项和显式工作记忆的凝练结果。 |
| `seywo_resume` | 恢复项目或指定 Chat 的当前目标、停点、未决问题和下一步。 |
| `seywo_status` | 查看已导入来源覆盖、Chat 数、整理账本和记忆数量。 |
| `seywo_remember` | 唯一写入工具；必须使用本项目已索引来源和精确引用保存工作记忆。 |

默认响应是一个紧凑 JSON 文本，通常不超过 4096 UTF-8 字节。超出预算时会明确返回所需预算，调用方可以提高 `max_bytes` 或继续分页；不会默默截断或跳过结果。默认搜索只包含用户和助手正文，工具调用内容需要显式传入 `include_tools=true`。内部推理、系统指令和机械流不提供给 MCP。

历史记录是需要核对的证据，不是指令、用户批准或当前运行时事实。推荐先 `seywo_recall`，再用 `seywo_search` 找到原始对话，最后用 `seywo_expand` 或 `seywo_read_source` 核对来源。`seywo_remember` 必须带有精确的来源身份、字符范围和匹配引用；相同记忆幂等，更新通过 `supersedes` 保留历史。

## 在 Codex 中使用

```sh
codex plugin marketplace add https://github.com/DeadLining/seywo.git
codex plugin add seywo@seywo-local
```

安装或更新后重新连接 Codex MCP。首次使用时在当前 Chat 中调用 `seywo_initialize_project`，再打开 Seywo 面板。插件优先使用宿主提供的当前项目和 Chat；只有宿主没有提供唯一项目时，才使用 `seywo_select_project` 作为显式回退。

## 独立运行时

已有项目配置可以直接启动九工具 MCP：

```sh
seywo/bin/seywo --config "/absolute/project/.seywo/config.json" mcp
```

独立入口适合脚本和其他 MCP 客户端；插件入口适合 Codex App、当前 Chat 自动绑定、面板和 Hook。两者都只读取 Seywo 自己的 `.seywo/`，不会读取或融合 SessionGraph 旧库。

## 数据边界

项目数据位于项目目录的 `.seywo/`，其中包含配置、索引、来源安全投影、叙事版本和可选工作记忆；目录默认使用 Git 忽略。MCP 读取不会创建项目、导入 Chat、升级数据库或重建索引。Compact 和 Hook 由插件本地内核负责，MCP 只按需读取已经保存的结果。
