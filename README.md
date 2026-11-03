# SessionGraph

Independent local desktop app for source-backed project memory. The React graph UI, bounded Rust IPC and Python extraction/MCP runtime are owned here. Gateway is an optional Responses provider, not the application host.

## Development

`npm ci`, `npm test`, `npm run build`, `npm run tauri:dev`.

For browser-only local reading, start `SESSIONGRAPH_DEV_CONFIG=/absolute/project/.sessiongraph/config.json npm run dev`. The opt-in development bridge serves only that project on loopback, with read-only allowlisted operations, bounded process lifetime and no settings writes or model calls. Production bundles contain no HTTP bridge.

`npm run app:build` creates the macOS bundle; `npm run app:install` installs it and switches the existing launcher with a backed-up, atomic registration update. Python 3.11+ with the `mcp` package is required; no dependencies are downloaded at runtime.

Project `.sessiongraph` directories, SQLite files and source evidence are not moved or re-extracted. The existing `~/Library/Application Support/local-ai-gateway/sessiongraph` directory is deliberately retained as the shared compatibility state location, including locks, queues and private credentials. Only the runtime source owner changes. Existing Hook/MCP command paths remain valid. Reconnect existing Codex MCP sessions after installation to load the new runtime.

The model form contains only BASE URL, API KEY and MODEL NAME. BASE URL is the Responses API prefix (for example `https://provider.example/v1`); saving appends `/responses`, and existing complete endpoints remain accepted. An unchanged existing endpoint is preserved exactly so endpoint-bound credentials keep working. API KEY left blank preserves credentials; existing environment-variable configuration remains internal and unchanged. HTTPS is required except for loopback HTTP. The key is stored locally with mode 0600, bound to the exact endpoint; it is never returned to the UI. An explicitly named environment key takes precedence. Legacy Codex token reuse remains limited to the exact matching loopback provider. Reads, graph browsing and migration make no model calls.

Gateway's Work Summary UI, timers, model-generation flow and native commands are retired. Gateway no longer packages runtime forwarding stubs; reconnect old MCP sessions through the stable launcher before upgrading Gateway. The legacy Work Summary cache and obsolete module documents have been moved to a recoverable private archive outside Gateway's active directories; user-exported reports remain untouched. Codex can use recall/search/expand to compose summaries from original evidence.

## Rollback

The installer preserves the previous app bundle and registration backups. Restore the previous `runtime.json` and launcher together from the matching runtime `backups` manifest; restore the previous app if needed. Do not delete project databases, queues or credentials. The Gateway source retains no SessionGraph implementation after extraction.

独立的**图谱压缩**机制已取消：无数量触发、历史候选扫描、专门的模型判别或手动压缩/撤销入口。写入时仍进行知识筛选和有证据的身份复用；冲突、纠正、替代与撤回机制保留。确认永久删除 chat 后按来源级联清理；仍有其他 chat 有效证据的节点保留，归档不等于删除。旧归并映射与回执兼容保留，不解散已有图谱身份。**存储维护**仍是独立、显式、零模型的载荷压缩与空闲页回收，不执行语义归并。

写入侧凝练现作为全局默认，已有启用项目和后续新项目均继承：在原有抽取请求中区分长期知识与过程记录，逐对象召回后再以单 token 判别身份；同时继承增量通知队列、有限恢复、有效修订、阶段控制和资源限额。项目明确设置的关闭项及预算优先，不改写各项目配置、不全量重抽历史、不增加读时模型。索引沿用项目本地 SQLite/FTS5，详细范围与真实验证见 `docs/condensation-mainline.json`。

图谱默认展示分为**知识层 / 过程记录 / 全部**。知识层只展示具有明确长期身份的事项；过程记录保存执行、活动及尚未形成稳定身份的历史事项。三者都是同一批 `work_memory` 与原文证据的只读投影，切换层次不会删除、改写或隐藏原文；搜索、`session_recall`、`session_search`、`session_expand` 仍覆盖完整历史。`sg_node_tiers` 只是影子映射，不替代修订账本，也不合并不同值或冲突断言。

历史缺失或旧规则分层现由 App 启动及现有 Hook 自动唤醒，在后台空闲时每批最多 100 条补齐，完成即退出；中断可续跑，不调用模型、不重抽 chat，也不删除节点或原文。已完成项目在后续 Hook 下不会写队列或启动分类进程。新发布对象仍在原有写入事务中同步分层；显式 `tier-graph` 审计入口保留。运行约束与回退见 `docs/tier-backfill.md`。
