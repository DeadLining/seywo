import { createRoot } from 'react-dom/client'
import { SessionGraphPanel } from './components/SessionGraphPanel'
import { RenderErrorBoundary } from './components/RenderErrorBoundary'
import './shell.css'
createRoot(document.getElementById('root')!).render(<main className="app-shell"><header className="app-header"><img src="/icon.png" alt=""/><h1>SessionGraph</h1><span>0.1.0</span></header><RenderErrorBoundary name="SessionGraph"><SessionGraphPanel /></RenderErrorBoundary></main>)
