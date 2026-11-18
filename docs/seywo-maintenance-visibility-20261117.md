# Seywo 后台整理提示与重试验收

2026-11-17。本次在原有 Compact 提炼、来源索引和主线发布逻辑之外补齐状态链路。

## 用户可见行为

- 面板按 Chat 显示等待、正在整理、完成、尚未完成、失败、受阻或中断。
- 失败显示固定的中文原因和显式重试入口，保留已发布主线；不直接展示异常、模型输出、凭据或路径。
- 活动任务每四秒读取状态，隐藏页面暂停，五分钟后转为手动刷新。
- 完成后更新主线；首次状态和主线并行读取时按发布版本核对，避免“已完成”旁仍显示较旧内容。
- 原生 SessionStart/UserPromptSubmit Hook 对同一失败提醒一次；队列/启动错误可在边界即时提示。Hook 始终正常退出。
- 原生轮次尚未结束时显示等待说明，不作为故障提醒，也不提供无效重试。

## 实现边界

每个 Chat 一份 `.seywo/maintenance/CHAT.json` 小记录，原子替换，无对话正文、模型响应、Compact 快照或追加日志。队列发布和任务终态按 inbox 锁排序，终态在释放 Chat 所有权前记录，外层退出不覆盖后继任务。通过现有内核文件锁识别中断，无心跳线程或心跳写盘；排队后六十秒未启动则可重试。

状态读取和提示不调用模型。显式重试只调度原有后台任务，复用持久边界与语义进度，实际提炼仍可能调用既有配置的模型。同 Chat 排队/运行中重复点击不重复调度。插件新增的 `seywo_retry_maintenance` 仅向 App 暴露；九个历史工具读写边界保持不变。`seywo_status` 的分页 Chat 行附带凝练整理状态。更新前已有的 inbox/Compact 失败账本可只读呈现，无数据库迁移。

Seywo Hook 的 Python 运行时尚未启动前发生的命令执行错误，由 Codex 自身负责提示；无法写入状态时尝试通过 Hook 原生提示报告。

## 验证证据

- Python 全量：817 passed，1 条既有 dateutil 弃用警告。`/tmp/seywo-maintenance-python-final-20261117.log`
- UI DOM：46 passed。`/tmp/seywo-maintenance-ui-final-20261117.log`
- Ruff、diff whitespace：通过。
- 隔离安装/真实 Codex MCP：通过，17 个工具，App-only 重试元数据正常。`/tmp/seywo-maintenance-plugin-final-20261117.json`
- 原生 Hook 生命周期：通过。真实 Codex app-server + 本地确定性模型夹具，正常 Pre/PostCompact 发布后注入一条安全失败状态，实际 Hook 输出被解析为恰好一条 warning；并不代表真实模型语义质量验收。`/tmp/seywo-maintenance-lifecycle-final-20261117.json`
- 浏览器：实际 panel.js，420 像素宽度的模拟数据场景，验证失败提示/重试恢复和主线保留；不是用户正在运行的 Codex 面板。截图：`docs/evidence/seywo-maintenance-failed-20261117.jpg`。

测试不调用用户的真实模型服务。真实已安装插件只读核验已通过：17 个工具、4 个已信任 Hook、整理状态可读取，0 次模型调用。另使用桌面版自带后端 codex-cli 0.144.0-alpha.4 验证原生警告解析。结构化证据见 `docs/evidence/seywo-maintenance-20261117.json`。本机部署与备份记录：`/Users/shengzhoukong/.local/share/seywo/deployments/20261117-142248-maintenance/deployment.json`。当前运行中的 Codex 未重启；用户重启后，仍需由真实 Chat 的下一次 Compact 验证运行时加载与实际任务结果。
