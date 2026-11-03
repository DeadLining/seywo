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
      'SessionGraph 独立为本地桌面应用和 Python 运行时，不再依赖 Gateway 提供界面。',
      '支持项目记忆、对象图谱、原文证据以及 MCP 回忆，并可切换知识层、过程记录和全部视图。',
      '模型设置只保留 BASE URL、API KEY 和 MODEL NAME。',
      '写入侧默认进行知识筛选与身份复用，保留冲突、修订、替代和撤回记录。',
      '永久删除 chat 后按来源级联清理；仍有其他有效证据的共享节点继续保留，归档不等于删除。',
      '存储维护只压缩载荷并回收空闲页，不执行语义归并或删除原文。',
      '新增版本更新日志入口。',
    ],
  },
]
