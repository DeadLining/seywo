import { useId, useLayoutEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

/** Native modal keeps keyboard focus and background interaction inside the dialog. */
export function SessionGraphDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()

  useLayoutEffect(() => {
    const dialog = dialogRef.current!
    const trigger = document.activeElement
    dialog.showModal()
    closeRef.current?.focus({ preventScroll: true })
    return () => {
      dialog.close()
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true })
    }
  }, [])

  return <dialog
    ref={dialogRef}
    className="sg-dialog"
    aria-modal="true"
    aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose() }}
    onClick={event => { if (event.target === event.currentTarget) onClose() }}
  >
    <header className="sg-dialog-heading">
      <h2 id={titleId}>{title}</h2>
      <button ref={closeRef} type="button" aria-label={`关闭${title}`} onClick={onClose}>
        <X size={18} aria-hidden="true"/>
      </button>
    </header>
    <div className="sg-dialog-content">{children}</div>
  </dialog>
}
