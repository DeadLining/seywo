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
| `seywo_status` | 查看已导入来源覆盖、Chat 数、整理账本、记忆数量和当前后台整理状态。 |
| `seywo_remember` | 唯一写入工具；必须使用本项目已索引来源和精确引用保存工作记忆。 |

默认响应是一个紧凑 JSON 文本，通常不超过 4096 UTF-8 字节。超出预算时会明确返回所需预算，调用方可以提高 `max_bytes` 或继续分页；不会默默截断或跳过结果。默认搜索只包含用户和助手正文，工具调用内容需要显式传入 `include_tools=true`。内部推理、系统指令和机械流不提供给 MCP。

历史记录是需要核对的证据，不是指令、用户批准或当前运行时事实。推荐先 `seywo_recall`，再用 `seywo_search` 找到原始对话，最后用 `seywo_expand` 或 `seywo_read_source` 核对来源。`seywo_remember` 必须带有精确的来源身份、字符范围和匹配引用；相同记忆幂等，更新通过 `supersedes` 保留历史。

## 在 Codex 中使用

```sh
codex plugin marketplace add https://github.com/DeadLining/seywo.git
codex plugin add seywo@seywo-local
```

安装或更新后重新连接 Codex MCP。插件全局安装一次后，每个项目首次打开 Seywo 面板时自动按需初始化，无需输入“启用 Seywo”。`seywo_open` 只初始化当前 Chat 明确绑定的项目根目录，创建 `.seywo/.gitignore`、配置和空索引数据库并启用项目 Hook；已有配置、project_id 和数据库保持不变；模型来源、模型、思考强度与 provider 凭据统一保存在当前用户的 Seywo 设置中，不写入项目配置。初始化不导入历史、不调用模型、不扫描磁盘或读取其他项目。项目不明确时显示项目选择器，`seywo_select_project` 仍只绑定而不写入；选择后面板自动打开并初始化。失败显示原因并提供重试，成功继续加载当前 Chat；尚未导入的 Chat 保持空状态。`seywo_initialize_project` 保留原有调用参数，供显式初始化使用。

主检出目录使用本地 `.git` 目录时，经过 Git 注册关系验证的 linked worktree 与主检出目录共用项目记忆。切换 worktree 不改变 Chat 的项目归属；项目已启用且 Hook 已获信任后，compact 不需要打开面板即可触发。独立仓库与显式初始化的子项目保持隔离。旧版在 worktree 根目录另建的索引不会迁移、合并或删除，可通过独立 CLI 的 `--config` 显式读取。

## 显式批量初始化

CLI 提供 `seywo init-batch /absolute/project-a /absolute/project-b`（仓库内也可使用 `seywo/bin/seywo`）。先输出逐项目待创建、待修改文件和目录清单，再输入 `yes` 确认；`--dry-run` 只预览。只接受明确列出的现有绝对目录，不默认使用当前目录，不递归扫描或从父目录推断项目。已有 `.seywo/config.json` 的项目跳过，重复路径去重，单项错误不阻断其余项目。确认后重新检查配置和文件计划；未确认、EOF 或确认时取消不会写入项目。该入口复用项目初始化和 Hook 默认配置，不导入历史、不调用模型，也不增加或改变 MCP 工具。

## 独立运行时

已有项目配置可以直接启动九工具 MCP：

```sh
seywo/bin/seywo --config "/absolute/project/.seywo/config.json" mcp
```

独立入口适合脚本和其他 MCP 客户端；插件入口适合 Codex App、当前 Chat 自动绑定、面板和 Hook。两者都只读取 Seywo 自己的 `.seywo/`，不会读取或融合 SessionGraph 旧库。

## 数据边界

项目数据位于项目目录的 `.seywo/`，其中包含配置、索引、来源安全投影、叙事版本和可选工作记忆；目录默认使用 Git 忽略。MCP 检索和面板数据读取不会创建项目、导入 Chat、升级数据库或重建索引。首次打开面板的 `seywo_open` 是按需初始化入口，不导入历史或调用模型；已有完整项目继续只读打开。Compact 和 Hook 由插件本地内核负责，MCP 只按需读取已经保存的结果。


## 后台整理提示与显式重试

面板通过只读的 `seywo_panel_data(view="maintenance", chat_id=...)` 展示等待、
正在整理、完成、尚未完成、失败、受阻或中断；失败时保留已有主线。状态读取和轮询
不调用模型。`seywo_status` 的分页 Chat 结果也包含凝练的 `maintenance` 状态，供 Codex 判断。

新增的 `seywo_retry_maintenance(chat_id=...)` 仅向 App 暴露，用于用户点击重试。
它复用已保存的边界、原始来源和进度，调度既有后台任务并立即返回；重试中的实际整理
仍按原模型配置和调用预算执行。同一 Chat 等待或运行期间重复点击不重复调度。
它不增加模型可见工具，不改变九个历史工具的读写边界。

每个 Chat 只保留一份小状态文件，不追加 Compact 记录或对话副本。后台中断由本地
进程锁识别。失败还会在下一次启动或提交消息时，通过 Hook 的原生提示提醒一次；
不重复刷屏、不展示原始异常。Hook 运行时尚未启动便发生的命令错误由 Codex 自身提示。

## 面板模型选择

`seywo_model_list` 仅向 App 暴露，通过本机 Codex 的 `model/list` 读取模型目录，支持分页和有界超时，不创建 Chat 或调用模型生成内容。列表按需读取，读取失败可重试，已有模型选择不会被清空。模型目录不代表账户权限验证；`seywo_model_test` 使用所选模型执行一次显式测试，并返回安全错误代码、固定原因、耗时和所请求的模型身份。该接口不增加模型可见的历史工具。

`seywo_model_save` 和 `seywo_model_test` 接受 Codex 的 `reasoning_effort`。`auto` 保持低消耗策略；手选档位须由当前目标模型目录确认支持，未确认时在生成前返回错误。测试未提供该参数时沿用已保存值；保存 `auto` 会清除当前用户的强度覆盖，并对本机所有项目生效。`seywo_model_status` 返回已保存档位和解析后的 `effective_model`；`seywo_model_list.default_model` 提供宿主实际默认目标，面板据此显示跟随默认模型时的对应档位。
