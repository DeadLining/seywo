import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SessionGraphCanvas } from './SessionGraphCanvas'
import type { WorkMap } from '../sessionGraph'
const map:WorkMap={revision:'one',nodes:[{id:'a',type:'memory',kind:'finding',label:'实验结果'},{id:'b',type:'memory',kind:'artifact',label:'训练记录'},{id:'c',type:'reference',kind:'path',label:'/tmp/report.json'}],edges:[{source:'a',target:'b',kind:'SUPPORTS'},{source:'a',target:'c',kind:'REFERENCES_PATH'}],chats:[],stats:{memories:2,total_memories:2,references:1,semantic_relations:1,reference_relations:1},notice:''}
beforeEach(()=>{
  vi.stubGlobal('PointerEvent',MouseEvent)
  vi.spyOn(SVGElement.prototype,'getBoundingClientRect').mockReturnValue({width:800,height:400,x:0,y:0,left:0,top:0,right:800,bottom:400,toJSON:()=>({})})
  SVGElement.prototype.setPointerCapture=vi.fn()
  SVGElement.prototype.hasPointerCapture=vi.fn().mockReturnValue(true)
  SVGElement.prototype.releasePointerCapture=vi.fn()
})
afterEach(()=>{vi.restoreAllMocks();vi.unstubAllGlobals()})
describe('SessionGraphCanvas navigation',()=>{
  it('pans on pointer drag and suppresses the resulting node click',()=>{
    const select=vi.fn();render(<SessionGraphCanvas map={map} focus="" onSelect={select}/>);
    const svg=screen.getByRole('img',{name:'事项关系图'}),node=screen.getByRole('button',{name:'实验结果'}),before=svg.getAttribute('viewBox')
    fireEvent.pointerDown(node,{button:0,clientX:100,clientY:100})
    fireEvent.pointerMove(svg,{clientX:180,clientY:140})
    expect(svg.getAttribute('viewBox')).not.toBe(before)
    expect(svg).toHaveClass('is-panning')
    fireEvent.pointerUp(svg);fireEvent.click(node)
    expect(select).not.toHaveBeenCalled();expect(svg).not.toHaveClass('is-panning')
    fireEvent.pointerDown(node,{button:0,clientX:100,clientY:100});fireEvent.pointerUp(node);fireEvent.click(node)
    expect(select).toHaveBeenCalledWith(map.nodes[0])
  })
  it('keeps tiny movements as clicks and ignores secondary buttons',()=>{
    const select=vi.fn();render(<SessionGraphCanvas map={map} focus="" onSelect={select}/>);
    const svg=screen.getByRole('img'),node=screen.getByRole('button',{name:'实验结果'}),before=svg.getAttribute('viewBox')
    fireEvent.pointerDown(svg,{button:2,clientX:10,clientY:10});fireEvent.pointerMove(svg,{clientX:90,clientY:90});expect(svg.getAttribute('viewBox')).toBe(before)
    fireEvent.pointerDown(node,{button:0,clientX:10,clientY:10});fireEvent.pointerMove(svg,{clientX:12,clientY:11});fireEvent.pointerUp(svg);fireEvent.click(node)
    expect(select).toHaveBeenCalledTimes(1)
  })
  it('zooms without growing the DOM canvas and restores fit after panning',()=>{
    render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>);
    const svg=screen.getByRole('img'),initial=svg.getAttribute('viewBox')
    fireEvent.click(screen.getByRole('button',{name:'放大图谱'}));expect(svg.getAttribute('viewBox')).not.toBe(initial)
    expect(svg.style.width).toBe('');expect(svg.style.height).toBe('')
    fireEvent.click(screen.getByRole('button',{name:'适应画布'}));expect(svg.getAttribute('viewBox')).toBe(initial)
    fireEvent.pointerDown(svg,{button:0,clientX:10,clientY:10});fireEvent.pointerMove(svg,{clientX:90,clientY:90});fireEvent.pointerCancel(svg)
    expect(svg).not.toHaveClass('is-panning')
  })
  it('supports keyboard navigation, bounded zoom and node activation',()=>{
    const select=vi.fn();render(<SessionGraphCanvas map={map} focus="a" onSelect={select}/>);
    const svg=screen.getByRole('img'),initial=svg.getAttribute('viewBox')
    fireEvent.keyDown(svg,{key:'ArrowLeft'});expect(svg.getAttribute('viewBox')).not.toBe(initial)
    fireEvent.keyDown(svg,{key:'0'});expect(svg.getAttribute('viewBox')).toBe(initial)
    for(let i=0;i<15;i++)fireEvent.click(screen.getByRole('button',{name:'放大图谱'}))
    expect(screen.getByRole('button',{name:'放大图谱'})).toBeDisabled()
    fireEvent.keyDown(screen.getByRole('button',{name:'训练记录'}),{key:'Enter'});expect(select).toHaveBeenCalledWith(map.nodes[1])
  })
  it('does not reset navigation when selection changes within the same graph',()=>{
    const {rerender}=render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>);
    const svg=screen.getByRole('img');fireEvent.click(screen.getByRole('button',{name:'放大图谱'}));const zoomed=svg.getAttribute('viewBox')
    rerender(<SessionGraphCanvas map={map} focus="a" onSelect={vi.fn()}/>);expect(svg.getAttribute('viewBox')).toBe(zoomed)
    rerender(<SessionGraphCanvas map={{...map,nodes:[],edges:[]}} focus="" onSelect={vi.fn()}/>);expect(svg.getAttribute('viewBox')).not.toContain('Infinity')
  })
})

it('highlights references on hover, handles capture loss and keyboard zoom',()=>{
  render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>);
  const svg=screen.getByRole('img'),node=screen.getByRole('button',{name:'实验结果'})
  fireEvent.pointerEnter(node);expect(screen.getByText('REFERENCES_PATH')).toBeInTheDocument()
  fireEvent.pointerLeave(node);expect(screen.queryByText('REFERENCES_PATH')).not.toBeInTheDocument()
  fireEvent.keyDown(svg,{key:'+'});fireEvent.keyDown(svg,{key:'-'});fireEvent.keyDown(svg,{key:'ArrowUp'});fireEvent.keyDown(svg,{key:'ArrowDown'});fireEvent.keyDown(svg,{key:'ArrowRight'})
  for(let i=0;i<15;i++)fireEvent.click(screen.getByRole('button',{name:'缩小图谱'}))
  expect(screen.getByRole('button',{name:'缩小图谱'})).toBeDisabled()
  fireEvent.pointerDown(svg,{button:0,clientX:0,clientY:0});fireEvent.pointerMove(svg,{clientX:40,clientY:40});fireEvent.lostPointerCapture(svg)
  expect(svg).not.toHaveClass('is-panning')
})


it('centers explicit relation navigation while retaining zoom and allowing subsequent pan',()=>{
  const {rerender}=render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>);
  fireEvent.click(screen.getByRole('button',{name:'放大图谱'}))
  const svg=screen.getByRole('img'), before=svg.getAttribute('viewBox')!.split(' ').map(Number)
  const request={id:'b',sequence:1}
  rerender(<SessionGraphCanvas map={map} focus="b" locate={request} onSelect={vi.fn()}/>);
  const node=screen.getByRole('button',{name:'训练记录'}), p=node.getAttribute('transform')!.match(/translate\(([^,]+),([^)]+)\)/)!
  const after=svg.getAttribute('viewBox')!.split(' ').map(Number)
  expect(after[0]+after[2]/2).toBeCloseTo(Number(p[1]))
  expect(after[1]+after[3]/2).toBeCloseTo(Number(p[2]))
  expect(after.slice(2)).toEqual(before.slice(2))
  fireEvent.keyDown(svg,{key:'ArrowRight'}); const moved=svg.getAttribute('viewBox')
  rerender(<SessionGraphCanvas map={map} focus="b" locate={request} onSelect={vi.fn()}/>);
  expect(svg.getAttribute('viewBox')).toBe(moved)
  rerender(<SessionGraphCanvas map={map} focus="b" locate={{id:'b',sequence:2}} onSelect={vi.fn()}/>);
  expect(svg.getAttribute('viewBox')).not.toBe(moved)
})


it('wheel zooms in small capped steps, anchored under the pointer including SVG letterboxing',()=>{
  render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>);
  const svg=screen.getByRole('img'), read=()=>svg.getAttribute('viewBox')!.split(' ').map(Number)
  const world=()=>{const [x,y,w,h]=read(),scale=Math.max(w/800,h/400);return [x+w/2+(620-400)*scale,y+h/2+(120-200)*scale]}
  const initial=read(),anchor=world()
  const wheel=new WheelEvent('wheel',{deltaY:-100,clientX:620,clientY:120,bubbles:true,cancelable:true})
  fireEvent(svg,wheel)
  expect(wheel.defaultPrevented).toBe(true)
  expect(screen.getByLabelText('缩放比例')).toHaveTextContent('102%')
  expect(read()[2]).toBeLessThan(initial[2])
  world().forEach((v,i)=>expect(v).toBeCloseTo(anchor[i]))
  fireEvent.wheel(svg,{deltaY:100,clientX:620,clientY:120})
  read().forEach((v,i)=>expect(v).toBeCloseTo(initial[i]))
  fireEvent.wheel(svg,{deltaY:-10000,clientX:620,clientY:120})
  expect(screen.getByLabelText('缩放比例')).toHaveTextContent('102%')
})
it('normalizes wheel units and ignores horizontal-only input and wheel during dragging',()=>{
  render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>);
  const svg=screen.getByRole('img'),initial=svg.getAttribute('viewBox')
  fireEvent.wheel(svg,{deltaY:0,deltaX:100});expect(svg.getAttribute('viewBox')).toBe(initial)
  fireEvent.wheel(svg,{deltaY:-3,deltaMode:1});expect(screen.getByLabelText('缩放比例')).toHaveTextContent('101%')
  fireEvent.click(screen.getByRole('button',{name:'适应画布'}))
  fireEvent.wheel(svg,{deltaY:-1,deltaMode:2});expect(screen.getByLabelText('缩放比例')).toHaveTextContent('102%')
  fireEvent.click(screen.getByRole('button',{name:'适应画布'}))
  fireEvent.pointerDown(svg,{button:0,clientX:20,clientY:20});fireEvent.wheel(svg,{deltaY:-100});expect(svg.getAttribute('viewBox')).toBe(initial)
  fireEvent.pointerUp(svg)
  for(let i=0;i<180;i++)fireEvent.wheel(svg,{deltaY:-100})
  expect(screen.getByRole('button',{name:'放大图谱'})).toBeDisabled()
  for(let i=0;i<180;i++)fireEvent.wheel(svg,{deltaY:100})
  expect(screen.getByRole('button',{name:'缩小图谱'})).toBeDisabled()
})
it('removes the non-passive wheel listener when canvas unmounts',()=>{
  const {unmount}=render(<SessionGraphCanvas map={map} focus="" onSelect={vi.fn()}/>),svg=screen.getByRole('img')
  const wheel=new WheelEvent('wheel',{deltaY:-100,cancelable:true});fireEvent(svg,wheel);expect(wheel.defaultPrevented).toBe(true)
  unmount();const detached=new WheelEvent('wheel',{deltaY:-100,cancelable:true});svg.dispatchEvent(detached);expect(detached.defaultPrevented).toBe(false)
})
