import packageInfo from '../package.json'

/** Single UI source of truth for the packaged application version. */
export const APP_VERSION = packageInfo.version

export type ChangelogEntry = {
  version: string
  items: string[]
}

/**
 * Keep entries newest-first. Add the next version here when the application
 * version changes in package.json and src-tauri/tauri.conf.json.
 */
export const CHANGELOG: ChangelogEntry[] = [
  {
    version: '0.1.0',
    items: [
      '新增「调用日志」弹窗：自动记录抽取、分类判断及模型连通性测试，展示模型、耗时、HTTP 状态、Token 用量与失败原因。',
      '调用日志支持按项目、模型和结果筛选，每页 50 条；区分服务响应成功与内容校验失败，用量缺失时显示未知。',
      '调用摘要保留最近 7 天，日志库主文件上限 20 MiB；不保存正文、模型输出或凭据，不进入图谱，查看日志不调用模型。',
      '日志写入失败不阻断原任务，永久删除 chat 时同步清理对应摘要，原有恢复证据继续独立保留。',
      'SessionGraph 独立为本地桌面应用和 Python 运行时，不再依赖 Gateway 提供界面。',
      '支持项目记忆、对象图谱、原文证据以及 MCP 回忆，并可切换知识层、过程记录和全部视图。',
      '模型设置只保留 BASE URL、API KEY 和 MODEL NAME，支持测试模型连通性与请求耗时。',
      '写入侧默认进行知识筛选与身份复用，保留冲突、修订、替代和撤回记录。',
      '永久删除 chat 后按来源级联清理；仍有其他有效证据的共享节点继续保留，归档不等于删除。',
      '存储维护只压缩载荷并回收空闲页，不执行语义归并或删除原文。',
      '历史事项在后台空闲时自动分批补齐分层，中断可续跑，不调用模型。',
      '已保存的 API KEY 默认显示掩码，可通过显示与隐藏按钮查看或遮蔽。',
      '设置与存储维护改为独立弹窗，内容在弹窗内滚动，不占用图谱页面。',
      '新增版本更新日志入口。',
    ],
  },
]
