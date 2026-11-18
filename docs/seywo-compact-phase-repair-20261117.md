# compact-v1 新阶段发布与失败重试修复

日期：2026-11-17。范围：当前仓库代码、自动回归、隔离插件检查。没有更新用户安装目录、重启 Codex、提交或推送 Git，也没有重跑用户当前 chat 的真实模型。

## 根因

1. 提示词只要求 branch/new_phase “尽量”提供锚点，生成端没有明确表达运行时的新用户目标约束。
2. 完整输出进入结构回放时才拒绝错误锚点，没有将错误反馈给模型修正。
3. 来源导入成功就将 inbox boundary 标成 done，混淆了来源持久化与语义发布。
4. 已有 maintenance receipt 可以掩盖 compact_batches 中的失败，空主线因此只显示未发布。

## 模型和结构契约

- `new_phase.anchor_index` 必须非空，引用当前输入片段中 `operation=new` 的事项索引。
- 事项必须 `attribution=user`，类型属于 `OBJECTIVE_TYPES`：goal、question、decision、correction、action。
- 该事项的 turn_indexes 必须与当前 segment 相交；运行时进一步检查锚点确实有当前 segment 中用户发言的来源证据。
- 索引共用 active_items / 虚拟新事项的身份空间，不能将 changes 数组位置直接当作全局索引。
- live validate 与结构回放均保留完整性约束；不自动将非法阶段切换改为 continue。
- 常规提示词维持 1800 字符以内（当前 1763），工具记录仍不进入模型输入。

## 有界修正

每个调度 chunk 正常调用一次模型。输出校验或结构回放失败后，最多再调用一次：发送相同 payload，加上具体 error_code、segment_index、anchor_index、reason 以及被拒绝的输出。错误信息和短标题输出用于修正，不再复制原始对话。

第二次仍失败则结束本轮 worker，保留 `compact_new_phase_requires_user_objective` 等具体错误，不发布版本、不提交 turn、不推进 delta_progress。所有尝试的调用次数与 usage 都计入 compact_batches。

修正成功只替换内存候选；所有 chunk 成功后才在同一 SQLite 事务写入 narrative、turn commits、progress 和成功批次。并发主线版本变更仍由原有 base-head 检查阻止覆盖。

## Inbox 与重试

```text
pending -> claimed -> 来源已导入（imported_snapshot_id + imported_cursor）
                         |
                         +-> 发布成功 / 已确认 no-op -> done
                         +-> 模型修正耗尽 -> failed -> Retry -> claimed
                         +-> 尾轮尚未结束 -> 保留 claimed，等待后续完整边界
```

- 导入标记保存在原 boundary 上，不新增一份源数据。重试验证该 snapshot 已在本项目数据库中，然后跳过再次导入。
- done 只能覆盖数据库已提交 cursor 范围内的来源；发布一个已结束的前缀不会误完成尚未结束的尾轮。
- 如果进程在数据库提交后、inbox 确认前中断，下一次根据 delta_progress 完成确认，模型调用为零，不重复生成版本。
- 来源导入失败保留原有有限重试；已导入数据的语义修正使用每次 worker 的模型调用预算，不受历史导入次数永久阻塞。
- 旧版本留下的 done boundary 不作为语义完成证据。即使 maintenance receipt 也错误显示 done，面板会查询 compact_batches 的失败，并允许 Retry 从未提交 cursor 读取已有来源。
- 成功后替换相同范围的失败状态，清除过期失败批次。再次运行已完成范围不会重复导入或生成 narrative。
- 删除无变化 Compact 自动补入的空 resolution_log，避免模型已确认无变化时仍生成无意义版本；已有字段保持原样。

## 失败可见性

维护状态保留固定、安全的中文原因：新阶段缺少本段用户目标的有效定位，模型修正后仍未通过校验，可以重试整理。不会展示原始异常或模型输出。

空主线明确显示“主线整理失败”，上方维护状态显示原因和可执行“重试整理”。状态与主线两个并行请求，无论哪个先返回都显示一致；进入 queued 后清除过期的失败空态。已有已发布主线继续展示。

沿用 `seywo_panel_data(view="maintenance")` 与 app-only `seywo_retry_maintenance`，不新增 MCP 工具。状态查询与 Retry 调度本身不调用模型。

## 验证

- 针对 Compact、inbox、maintenance：296 项通过。
- 完整 Python：834 项通过；仅一个已有的 dateutil UTC API 弃用警告。
- 面板：47 项通过。
- 前端打包成功；Ruff 与 `git diff --check` 通过。
- 隔离插件检查通过，17 个工具；没有替换当前安装的插件。

新增 `seywo/tests/test_compact_phase_repair.py` 共 17 个回归用例，覆盖旧目标、缺少锚点、非目标类型、助手归属、跨片段锚点、缺少本片段用户证据、修正成功、耗尽后重试、旧版矛盾回执、发布前不确认、提交后中断、未闭合尾轮、no-op、空面板 Retry、11 轮 / 5 主题一次发布以及历史重试次数耗尽后恢复。来源行数、版本数和 turn commits 均有真实 SQLite 断言。

自动验证使用确定性模型 fixture；不能将其视为当前线上模型在用户实际 chat 上已经完成发布的证据。

日志：

- `/tmp/seywo-phase-target-final.log`
- `/tmp/seywo-phase-python-final.log`
- `/tmp/seywo-phase-ui-final.log`
- `/tmp/seywo-phase-build.log`
- `/tmp/seywo-phase-lint-final.log`
- `/tmp/seywo-phase-plugin-final.json`
