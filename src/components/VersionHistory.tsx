import { useEffect, useRef, useState } from 'react'
import { History, X } from 'lucide-react'
import { APP_VERSION, CHANGELOG } from '../changelog'

export function VersionHistory() {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const restoreFocus = useRef(false)

  useEffect(() => {
    if (!open) {
      if (restoreFocus.current) triggerRef.current?.focus()
      restoreFocus.current = false
      return
    }
    restoreFocus.current = true
    closeRef.current?.focus()
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      setOpen(false)
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open])

  return <div className="app-version">
    <span className="app-version-label">{APP_VERSION}</span>
    <button
      ref={triggerRef}
      type="button"
      className="app-changelog-trigger"
      aria-haspopup="dialog"
      aria-expanded={open}
      onClick={() => setOpen(true)}
    >
      <History size={12} aria-hidden="true"/>
      更新日志
    </button>
    {open && <div
      className="app-changelog-backdrop"
      onClick={event => { if (event.target === event.currentTarget) setOpen(false) }}
    >
      <section
        className="app-changelog-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sessiongraph-changelog-title"
      >
        <header>
          <div>
            <h2 id="sessiongraph-changelog-title">Seywo 版本更新日志</h2>
            <p>当前版本 {APP_VERSION}</p>
          </div>
          <button ref={closeRef} type="button" className="app-changelog-close" aria-label="关闭更新日志" onClick={() => setOpen(false)}>
            <X size={16} aria-hidden="true"/>
          </button>
        </header>
        <div className="app-changelog-entries">
          {CHANGELOG.map(entry => <article key={entry.version} className="app-changelog-entry">
            <div className="app-changelog-entry-title">
              <h3>{entry.version}</h3>
              {entry.version === APP_VERSION && <span>当前版本</span>}
            </div>
            <ul>{entry.items.map((item, index) => <li key={index}>{item}</li>)}</ul>
          </article>)}
          {!CHANGELOG.some(entry => entry.version === APP_VERSION) && <p className="app-changelog-empty">当前版本暂未记录更新说明。</p>}
        </div>
      </section>
    </div>}
  </div>
}
