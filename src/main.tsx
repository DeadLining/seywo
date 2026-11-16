import { createRoot } from 'react-dom/client'
import { TaskWorkspace } from './components/TaskWorkspace'
import { RenderErrorBoundary } from './components/RenderErrorBoundary'
import './shell.css'
createRoot(document.getElementById('root')!).render(<main className="app-shell"><RenderErrorBoundary name="Seywo"><TaskWorkspace /></RenderErrorBoundary></main>)
