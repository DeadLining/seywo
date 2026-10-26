import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'
const root = path.resolve(import.meta.dirname, '..')
const resources = {'../runtime/entrypoint.py':'sessiongraph/entrypoint.py'}
for (const folder of ['sessiongraph','hooks','scripts']) for (const file of fs.readdirSync(`${root}/runtime/${folder}`)) {
  if (/\.(py|sh)$/.test(file) || file === 'policy.json') resources[`../runtime/${folder}/${file}`] = `sessiongraph/${folder}/${file}`
}
fs.writeFileSync(`${root}/src-tauri/tauri.conf.json`,JSON.stringify({$schema:'https://schema.tauri.app/config/2',productName:'SessionGraph',version:'0.1.0',identifier:'com.sessiongraph.app',build:{beforeDevCommand:'npm run dev',devUrl:'http://127.0.0.1:1432',beforeBuildCommand:'npm run build',frontendDist:'../dist'},app:{macOSPrivateApi:true,windows:[{title:'SessionGraph',width:1180,height:800,minWidth:640,minHeight:540}],security:{csp:"default-src 'self'; connect-src 'self' ipc: http://ipc.localhost; img-src 'self' asset: http://asset.localhost data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'"}},bundle:{active:true,targets:['app'],icon:['icons/icon.png'],resources}},null,2)+'\n')
const crc = bytes => {let c=0xffffffff;for(const byte of bytes){c^=byte;for(let k=0;k<8;k++)c=(c>>>1)^((c&1)?0xedb88320:0)}return (c^0xffffffff)>>>0}
const chunk = (type, data) => {const t=Buffer.from(type),b=Buffer.alloc(data.length+12);b.writeUInt32BE(data.length);t.copy(b,4);data.copy(b,8);b.writeUInt32BE(crc(Buffer.concat([t,data])),data.length+8);return b}
const size=512, pixels=Buffer.alloc(size*size*4)
const points=[[128,256,36,[43,139,106]],[256,128,45,[43,139,106]],[384,256,36,[61,111,154]],[256,384,45,[199,144,53]],[256,256,29,[43,139,106]]]
for(let y=0;y<size;y++)for(let x=0;x<size;x++){
  let color=[244,248,246];
  for(const [a,b] of [[0,1],[1,2],[2,3],[3,0],[0,4],[2,4]]){const [ax,ay]=points[a],[bx,by]=points[b],t=Math.max(0,Math.min(1,((x-ax)*(bx-ax)+(y-ay)*(by-ay))/((bx-ax)**2+(by-ay)**2)));if(Math.hypot(x-ax-t*(bx-ax),y-ay-t*(by-ay))<8)color=[154,183,171]}
  for(const [cx,cy,r,c] of points)if(Math.hypot(x-cx,y-cy)<r)color=c
  pixels.set([...color,255],(y*size+x)*4)
}
const rows=Buffer.alloc((size*4+1)*size);for(let y=0;y<size;y++)pixels.copy(rows,y*(size*4+1)+1,y*size*4,(y+1)*size*4)
const ihdr=Buffer.alloc(13);ihdr.writeUInt32BE(size);ihdr.writeUInt32BE(size,4);ihdr[8]=8;ihdr[9]=6
const png=Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]),chunk('IHDR',ihdr),chunk('IDAT',zlib.deflateSync(rows)),chunk('IEND',Buffer.alloc(0))])
fs.mkdirSync(`${root}/src-tauri/icons`,{recursive:true});fs.mkdirSync(`${root}/public`,{recursive:true})
fs.writeFileSync(`${root}/src-tauri/icons/icon.png`,png);fs.writeFileSync(`${root}/public/icon.png`,png)
