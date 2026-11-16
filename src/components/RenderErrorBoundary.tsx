import { Component, type ErrorInfo, type ReactNode } from 'react'

type Props = { children: ReactNode; name: string }
type State = { error: Error | null }

/** Keep a render failure visible and recoverable instead of unmounting the window. */
export class RenderErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: unknown): State {
    return { error: error instanceof Error ? error : new Error(String(error)) }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[Gateway UI: ${this.props.name}]`, error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return <section role="alert" style={{ padding: 24, fontSize: 14 }}>
      <h2>{this.props.name}暂时无法显示</h2>
      <p>页面暂时无法显示，请重试。此操作不会触发模型抽取。</p>
      <button type="button" onClick={() => this.setState({ error: null })}>重试显示</button>
    </section>
  }
}
