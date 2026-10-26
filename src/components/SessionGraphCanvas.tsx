import { useId, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { graphLayout, graphWindow, revisionValidityLabel, type GraphNode, type WorkMap } from '../sessionGraph'

type Camera = { key: string; x: number; y: number; width: number; height: number; zoom: number }
type Drag = { id: number; x: number; y: number; camera: Camera; scale: number; moved: boolean }
const kinds: Record<string, string> = { goal: '目标', decision: '决策', finding: '发现', artifact: '产物', progress: '进度', next_step: '下一步', constraint: '约束' }
const referenceEdge = (kind: string) => ['MENTIONS', 'REFERENCES_PATH'].includes(kind)

function titleLines(title: string) {
  const lines = ['']; let width = 0
  for (const ch of title) {
    const advance = ch.charCodeAt(0) > 255 ? 1 : .55
    if (width + advance > 12) {
      if (lines.length === 2) { lines[1] = lines[1].slice(0, -1) + '…'; break }
      lines.push(''); width = 0
    }
    lines[lines.length - 1] += ch; width += advance
  }
  return lines
}

export function SessionGraphCanvas({ map, focus, locate, onSelect }: { map: WorkMap; focus: string; locate?: { id: string; sequence: number } | null; onSelect: (node: GraphNode) => void }) {
  const svgRef = useRef<SVGSVGElement>(null)
  const marker = useId().replace(/:/g, '')
  const view = useMemo(() => graphWindow(map.nodes, map.edges, focus), [map.nodes, map.edges, focus])
  // Selection should not move an otherwise unchanged graph under the pointer.
  const key = JSON.stringify([view.nodes.map(n => n.id).sort(), view.edges.map(e => `${e.source}:${e.kind}:${e.target}`).sort()])
  const points = useMemo(() => graphLayout([...view.nodes].sort((a, b) => a.id.localeCompare(b.id)), [...view.edges].sort((a, b) => `${a.source}:${a.target}`.localeCompare(`${b.source}:${b.target}`))), [key])
  const fit = useMemo<Camera>(() => {
    const all = [...points.values()]
    if (!all.length) return { key, x: 0, y: 0, width: 480, height: 320, zoom: 1 }
    const x = Math.min(...all.map(p => p.x)) - 120, y = Math.min(...all.map(p => p.y)) - 85
    return { key, x, y, width: Math.max(300, Math.max(...all.map(p => p.x)) + 120 - x), height: Math.max(220, Math.max(...all.map(p => p.y)) + 85 - y), zoom: 1 }
  }, [points, key])
  const [savedCamera, setCamera] = useState<Camera | null>(null)
  const camera = savedCamera?.key === key ? savedCamera : fit
  // Explicit relation navigation centers the target; ordinary selection must not
  // disturb a manually panned/zoomed camera. Repeated requests are intentional.
  useLayoutEffect(() => {
    if (!locate) return
    const point = points.get(locate.id)
    if (!point) return
    setCamera(previous => {
      const base = previous?.key === key ? previous : fit
      return { ...base, x: point.x - base.width / 2, y: point.y - base.height / 2 }
    })
  }, [locate, key, fit, points])
  const [panning, setPanning] = useState(false)
  const [hover, setHover] = useState('')
  const drag = useRef<Drag | null>(null), suppressClick = useRef(false)
  // Native non-passive binding is required: React wheel listeners can be passive.
  // Small trackpad deltas stay small; even a coarse wheel event is capped at 2%.
  useLayoutEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    function wheel(event: WheelEvent) {
      if (!Number.isFinite(event.deltaY) || event.deltaY === 0) return
      event.preventDefault()
      if (drag.current) return
      const rect = svg!.getBoundingClientRect()
      if (!rect.width || !rect.height) return
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.height : 1)
      const delta = Math.max(-100, Math.min(100, pixels))
      const factor = Math.exp(-delta * Math.log(1.02) / 100)
      setCamera(previous => {
        const base = previous?.key === key ? previous : fit
        const zoom = Math.max(.5, Math.min(3, base.zoom * factor))
        if (zoom === base.zoom) return base
        const ratio = base.zoom / zoom, width = base.width * ratio, height = base.height * ratio
        // Account for xMidYMid meet letterboxing, keeping the cursor's world point fixed.
        const scale = Math.max(base.width / rect.width, base.height / rect.height)
        const dx = (event.clientX - rect.left - rect.width / 2) * scale
        const dy = (event.clientY - rect.top - rect.height / 2) * scale
        return { ...base, zoom, width, height,
          x: base.x + (base.width - width) / 2 + dx * (1 - ratio),
          y: base.y + (base.height - height) / 2 + dy * (1 - ratio) }
      })
    }
    svg.addEventListener('wheel', wheel, { passive: false })
    return () => svg.removeEventListener('wheel', wheel)
  }, [key, fit])
  const active = hover || focus
  const neighbors = new Set([active])
  for (const e of view.edges) { if (e.source === active) neighbors.add(e.target); if (e.target === active) neighbors.add(e.source) }

  function zoomBy(delta: number) {
    const zoom = Math.max(.5, Math.min(3, camera.zoom + delta))
    const width = fit.width / zoom, height = fit.height / zoom
    setCamera({ ...camera, x: camera.x + (camera.width - width) / 2, y: camera.y + (camera.height - height) / 2, width, height, zoom })
  }
  function pointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    if (event.button !== 0 || drag.current) return
    const rect = event.currentTarget.getBoundingClientRect()
    if (!rect.width || !rect.height) return
    suppressClick.current = false
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, camera, scale: Math.max(camera.width / rect.width, camera.height / rect.height), moved: false }
  }
  function pointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const current = drag.current
    if (!current || current.id !== event.pointerId) return
    const dx = event.clientX - current.x, dy = event.clientY - current.y
    if (!current.moved && Math.hypot(dx, dy) < 4) return
    if (!current.moved) { event.currentTarget.setPointerCapture(event.pointerId); current.moved = true; setPanning(true) }
    event.preventDefault(); suppressClick.current = true
    setCamera({ ...current.camera, x: current.camera.x - dx * current.scale, y: current.camera.y - dy * current.scale })
  }
  function endDrag(event: ReactPointerEvent<SVGSVGElement>) {
    if (drag.current?.id !== event.pointerId) return
    drag.current = null; setPanning(false)
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  return <div className="sg-graph-shell">
    <div className="sg-graph-controls"><span className="sg-pan-hint">按住拖动 · 滚轮微调缩放</span><div className="sg-zoom">
      <button aria-label="缩小图谱" disabled={camera.zoom <= .5} onClick={() => zoomBy(-.25)}>−</button>
      <output aria-label="缩放比例">{Math.round(camera.zoom * 100)}%</output>
      <button aria-label="放大图谱" disabled={camera.zoom >= 3} onClick={() => zoomBy(.25)}>+</button>
      <button aria-label="适应画布" onClick={() => setCamera(fit)}>适应</button>
    </div></div>
    <div className="sg-viewport">
      <svg ref={svgRef} className={`sg-graph${panning ? ' is-panning' : ''}`} role="img" aria-label="事项关系图" tabIndex={0}
        viewBox={`${camera.x} ${camera.y} ${camera.width} ${camera.height}`}
        onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={endDrag} onPointerCancel={endDrag}
        onLostPointerCapture={() => { drag.current = null; setPanning(false) }}
        onClickCapture={event => { if (suppressClick.current) { event.preventDefault(); event.stopPropagation(); suppressClick.current = false } }}
        onKeyDown={event => {
          if (event.target !== event.currentTarget) return
          const shift = camera.width * .08
          if (event.key.startsWith('Arrow')) {
            event.preventDefault()
            setCamera({ ...camera, x: camera.x + (event.key === 'ArrowLeft' ? -shift : event.key === 'ArrowRight' ? shift : 0), y: camera.y + (event.key === 'ArrowUp' ? -shift : event.key === 'ArrowDown' ? shift : 0) })
          } else if (['+', '=', '-', '0'].includes(event.key)) {
            event.preventDefault(); if (event.key === '0') setCamera(fit); else zoomBy(event.key === '-' ? -.25 : .25)
          }
        }}>
        <defs><marker id={marker} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M1,1 L7,4 L1,7" fill="none" stroke="currentColor" strokeWidth="1.4" /></marker></defs>
        {view.edges.map((edge, i) => {
          const a = points.get(edge.source)!, b = points.get(edge.target)!, dx = b.x - a.x, dy = b.y - a.y
          const t = Math.min(.45, Math.min(84 / (Math.abs(dx) || 1), 34 / (Math.abs(dy) || 1)))
          const reference = referenceEdge(edge.kind), highlighted = active === edge.source || active === edge.target
          return <g key={`${edge.source}-${edge.target}-${i}`} className={`sg-edge ${reference ? 'reference' : 'semantic'}${highlighted ? ' highlighted' : active ? ' muted' : ''}`}>
            <line x1={a.x + dx * t} y1={a.y + dy * t} x2={b.x - dx * t} y2={b.y - dy * t} markerEnd={reference ? undefined : `url(#${marker})`} />
            {(!reference || highlighted) && <text className="sg-edge-label" x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 6} textAnchor="middle">{edge.kind}{edge.statement_mode && edge.statement_mode !== 'asserted' ? ` · ${edge.statement_mode}` : ''}</text>}
          </g>
        })}
        {view.nodes.map(node => {
          const p = points.get(node.id)!, reference = node.type === 'reference', lines = titleLines(node.label)
          return <g key={node.id} role="button" aria-label={node.label} aria-pressed={focus === node.id} tabIndex={0}
            className={`sg-node ${reference ? 'reference' : 'memory'}${focus === node.id ? ' selected' : ''}${active && !neighbors.has(node.id) ? ' muted' : ''}`}
            onPointerEnter={() => { if (!drag.current) setHover(node.id) }} onPointerLeave={() => setHover('')}
            onClick={() => onSelect(node)} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(node) } }}
            transform={`translate(${p.x},${p.y})`}>
            <title>事项：{node.label}{node.validity ? ` · ${revisionValidityLabel(node.validity)}` : ''}</title>
            <rect className="sg-node-surface" x="-82" y="-33" width="164" height="66" rx="7" />
            <circle className="sg-node-dot" cx="-66" cy="-18" r="3" />
            <text className="sg-node-kind" x="-57" y="-14">{reference ? (node.kind === 'path' ? '文件引用' : '概念引用') : node.validity && node.validity.state !== 'current' ? revisionValidityLabel(node.validity) : (node.object_type || kinds[node.kind] || node.kind)}</text>
            {lines.map((line, i) => <text key={i} className="sg-node-title" x="-66" y={5 + i * 15}>{line}</text>)}
          </g>
        })}
      </svg>
    </div>
    <div className="sg-graph-caption"><span><i className="sg-line-key semantic" />语义声明（待核实）</span><span><i className="sg-line-key" />字面引用</span><span>{view.nodes.length} / {map.nodes.length} 节点</span></div>
    {view.omitted > 0 && <p className="sg-omitted">当前显示 {view.nodes.length} / {map.nodes.length} 个节点；搜索或切换列表定位其余事项。</p>}
  </div>
}
