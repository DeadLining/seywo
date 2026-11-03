# 历史分层自动补齐

App 启动检查已登记且身份匹配的项目；现有 SessionStart、PostCompact、Stop、SessionEnd Hook 也会唤醒有缺口的项目。MCP/UI 的读取路径不创建任务、不写数据库、不调用模型。

`runtime/sessiongraph/tier_backfill.py` 是后台补齐的唯一调度入口，复用 `tiering.project` 和现行 tiering-v3 规则。每批最多 100 条，SQLite 指令时间预算 2 秒，只更新缺失或旧版本分层。不重新抽取 chat，不合并或删除节点、关系、原文或证据。

写入使用 projection_only Store，跳过普通写入连接的 schema、存储、原文与读取索引迁移；关闭连接也不刷新其他派生索引。短事务持有 update.lock、stage-activity.lock 与 graph-v2.lock；项目忙时让路，最多按 1、2、4、8、16、30 秒等待，之后留待下一次 Hook/App 启动唤醒。单一忙项目不阻塞其他项目的一轮处理。

分层表里的当前版本记录就是断点：事务中断自动回滚，已提交批次保留。元数据队列位于运行时的 tier-backfill/queue.json，只记录配置路径、唤醒标识与状态，不存原文。等待项在下次事件重试，损坏项目标记 blocked，其他项目继续。任务完成即移出队列并退出；已经完成的项目在后续 Hook 下不写队列、不启动子进程。

App 启动任务若完成了补齐，会发送一次完成事件刷新界面；Hook 在独立进程中完成的进度可用现有“刷新”按钮查看。进度提示不再把待分类当作红色错误。

回退：安装之前的 App 备份即可停止新唤醒入口；已有分层是兼容的派生映射，可以保留，节点与证据无需恢复。不通过回滚删除数据库、图谱或用户配置。

验证入口：`runtime/tests/test_tier_backfill.py` 验证分批、中断续跑、锁竞争、幂等、进程单实例、队列完成退出、丢失唤醒和事实表不变；既有 tiering、Hook、只读接口、stage lease 与前端回归继续适用。当前执行状态记录在 tier-backfill-execution.json。
