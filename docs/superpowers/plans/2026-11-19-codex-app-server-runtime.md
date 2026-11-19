# Seywo 0.5.0 模型运行时替换

目标：默认 Codex 后端完整切换到 app-server，保留 compact 领域契约、项目数据与 Responses 后端。先检查工作区原有修改，再分层实现、测试、打包和替换本机插件。

## 分工与调用链

```text
模型设置 / maintain → compact_pipeline
                         ↓
                   CodexTransport
                         ↓
             CodexAppServerWorker（串行、进程内复用）
                         ↓
             RpcConnection → ProcessScope
                         ↓
               codex app-server --stdio
                 ├─ model/list、account/*：无生成
                 └─ 临时 Thread → 一次 turn/start → 释放 Thread
```

- `codex_transport.py`：保持调用方接口；选择模型、提供方和支持的思考强度。
- `codex_app_server.py`：Worker、目录缓存、隔离 Thread、账号授权与错误恢复。
- `codex_rpc.py`：有界 JSON-RPC、早到通知、写入期限、队列字节限额和清理。
- `codex_runtime.py`：启动配置、公开目录字段、固定错误分类。
- `compact_pipeline.py`：继续负责 Schema/语义校验、有界 repair、来源和主线原子发布；按实际提交次数记录用量。
- MCP 与后台整理分别拥有进程内 Worker，不引入跨进程守护服务。

## 约束

1. 生成不再调用 `codex exec`，授权不调用 `codex login status`；不读 App 私有令牌。
2. 仍使用宿主 Codex 可执行文件；不声称直接复用当前 App Chat 的模型会话或临时模型覆盖。
3. 每次生成隔离上下文；运行时不隐式重放；领域 repair 继续显式、有界计数。
4. 临时空目录、只读沙箱；关闭 Hook、插件、MCP、技能、记忆、Shell、图片、目标管理和搜索。宿主可能保留用户输入及旧版 apply_patch 工具定义，调用即拒绝。
5. Schema 仅通过 `outputSchema` 传入一次。无 Schema/输出临时文件，无持久化分析对话。
6. 单行 2 MiB、累计读取/排队/写入各有 4 MiB 限额；队列最多 256 条；生成结果最多 64 KiB。时间限额包括串行等待。
7. 授权不足且尚未提交模型时记 0 次调用；保留尚未完成的浏览器授权。失败不推进语义游标、不重复导入或发布。
8. 不把只读沙箱、Schema 或字节限制描述成模型输出 Token 硬上限，也不承诺零宿主上下文开销。

## 实施检查

- [x] 阅读原有未提交修改；CodeGraph 未覆盖新目录时直接检查源码。
- [x] 替换生成、目录和账号路径；移除旧 exec/login 探测及中间文件链路。
- [x] 回归早到事件、工具拒绝、超时中断、进程重建、并发、畸形协议、闲置队列限额、缓存失效和授权流程。
- [x] 接入面板授权入口、固定错误提示；更新面板资源 URI、重建 HTML。
- [x] 回归 compact 发布/repair/失败可重试/幂等；修正预检失败的真实模型调用计数。
- [x] 真实模型连通与隔离项目 compact；本地假提供方抓取真实宿主请求。
- [x] 隔离安装验收：19 工具、5 Hook、MCP/面板资源/来源检索链路通过。
- [x] 最终完整 Python/UI 测试、分发包和本机安装核对。

## 已取得证据

- 真实模型：`crs / gpt-6.1-sol / low`；两次探测输入均 5,728 token，输出 15 / 29 token，耗时 4.832 / 9.181 秒。
- 隔离项目真实 compact：2 轮 → 1 节点 → 版本 1；1 次调用，7,224 输入 / 446 输出 token；重复处理 0 调用，来源和主线版本数量不变。
- 真实宿主 + 假提供方：两次生成无技能目录注入，仅剩宿主用户输入工具定义；无持久化 transcript。
- 此机器的真实模型使用 API 提供方；浏览器 OAuth 全流程及原生 Windows/Linux 尚未实测。自动化账号协议/平台契约测试不能替代这些验收。
- PyPI 在线依赖下载遇到 TLS 错误；已有依赖缓存的 `UV_OFFLINE=1` 可移植入口/离线 Hook 测试通过，不能据此声称新机器在线安装已验证。

## 复核命令

```sh
UV_OFFLINE=1 pytest -q seywo/tests
node seywo/ui/build.mjs
node --test seywo/ui/panel.test.mjs
python3 scripts/verify-seywo-app-server.py
UV_OFFLINE=1 python3 scripts/verify-seywo-plugin.py --report /tmp/seywo-plugin-verification.json
python3 scripts/package-seywo-plugin.py --output /tmp/seywo-package
```

## 最终交付状态

- Python：915 通过、2 跳过；UI：62 通过。保留依赖弃用/模块类型提示，未影响结果。
- 宿主协议：当前 App 0.169.0-alpha.11.1 和 PATH 0.152.1 均通过本地假提供方检查；旧版保留 apply_patch 定义，Worker 拒绝实际调用。
- 已安装：`~/.codex/plugins/cache/seywo-local/seywo/0.5.0`。核心源码、manifest 和 HTML 与仓库逐字节一致。
- 当前 App 新连接检查：19 工具、主线/维护状态读取通过，模型调用 0。报告 `/tmp/seywo-0.5.0-installed-verification.json`。
- 分发包：`/tmp/seywo-0.5.0-final-20261119/seywo-plugin-0.5.0.zip`，80 文件、393872 字节。
- 生效条件：已有 Chat 的 MCP 需重新连接；版本路径变化后五个 Hook 均为 modified，须用户在宿主重新审核。未自行授予信任，未宣称自动 Compact 已生效。
- 保留未提交修改；本次未提交或推送 main。
