import fs from 'node:fs'
import path from 'node:path'
import './generate-icons.mjs'
const root = path.resolve(import.meta.dirname, '..')
const resources = {'../runtime/entrypoint.py':'sessiongraph/entrypoint.py'}
for (const file of fs.readdirSync(`${root}/seywo/src/seywo`)) if (file.endsWith('.py')) resources[`../seywo/src/seywo/${file}`] = `sessiongraph/seywo/${file}`
for (const folder of ['sessiongraph','hooks','scripts']) for (const file of fs.readdirSync(`${root}/runtime/${folder}`)) {
  if (/\.(py|sh)$/.test(file) || file === 'policy.json') resources[`../runtime/${folder}/${file}`] = `sessiongraph/${folder}/${file}`
}
fs.writeFileSync(`${root}/src-tauri/tauri.conf.json`,JSON.stringify({$schema:'https://schema.tauri.app/config/2',productName:'Seywo',version:'0.1.0',identifier:'com.sessiongraph.app',build:{beforeDevCommand:'npm run dev',devUrl:'http://127.0.0.1:1432',beforeBuildCommand:'npm run build',frontendDist:'../dist'},app:{macOSPrivateApi:true,windows:[{title:'Seywo',width:1180,height:800,minWidth:640,minHeight:540}],security:{csp:"default-src 'self'; connect-src 'self' ipc: http://ipc.localhost; img-src 'self' asset: http://asset.localhost data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'"}},bundle:{active:true,targets:['app'],icon:['icons/icon.png','icons/icon.ico'],resources,windows:{nsis:{installMode:'currentUser'}}}},null,2)+'\n')
