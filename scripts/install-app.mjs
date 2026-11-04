import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'
import { execFileSync } from 'node:child_process'
const root=path.resolve(import.meta.dirname,'..')
const built=path.join(root,'src-tauri/target/release/bundle/macos/SessionGraph.app')
const target='/Applications/SessionGraph.app'
const state=path.join(os.homedir(),'Library/Application Support/local-ai-gateway/sessiongraph')
const python=process.env.SESSIONGRAPH_PYTHON||'/Library/Frameworks/Python.framework/Versions/3.12/bin/python3'
execFileSync(python,['-B','-c','import sys,mcp; assert sys.version_info >= (3,11)'])
execFileSync('/usr/bin/codesign',['--verify','--deep','--strict',built])
const stage=`${target}.stage-${process.pid}`
const backup=`${target}.backup-${Date.now()}`
let replaced=false
const executable=path.join(target,'Contents/MacOS/sessiongraph-app')
const running=execFileSync('ps',['-axo','pid=,command='],{encoding:'utf8'}).split('\n').flatMap(line=>{
  const match=line.trim().match(/^(\d+)\s+(.+)$/)
  return match && match[2]===executable ? [Number(match[1])] : []
})
try {
  fs.cpSync(built,stage,{recursive:true})
  execFileSync('/usr/bin/codesign',['--verify','--deep','--strict',stage])
  if(fs.existsSync(target)) { fs.renameSync(target,backup); replaced=true }
  fs.renameSync(stage,target)
  const source=path.join(target,'Contents/Resources/sessiongraph')
  const status=JSON.parse(execFileSync(python,['-B',`${source}/entrypoint.py`,'runtime-status'],{encoding:'utf8'}))
  if(status.model_calls!==0||!status.mcp_version)throw new Error('Runtime preflight failed')
  // Immutable source protects MCP processes from future in-place app replacement.
  const hash=crypto.createHash('sha256')
  function entries(dir,prefix='') {return fs.readdirSync(dir,{withFileTypes:true}).sort((a,b)=>a.name.localeCompare(b.name)).flatMap(e=>e.isDirectory()?entries(`${dir}/${e.name}`,`${prefix}${e.name}/`):[`${prefix}${e.name}`])}
  for(const file of entries(source))hash.update(file).update(fs.readFileSync(`${source}/${file}`))
  const runtime=path.join(state,'runtimes',hash.digest('hex').slice(0,20))
  if(!fs.existsSync(runtime)) {
    fs.mkdirSync(path.dirname(runtime),{recursive:true,mode:0o700})
    const temp=`${runtime}.stage-${process.pid}`
    fs.cpSync(source,temp,{recursive:true});fs.renameSync(temp,runtime)
  }
  execFileSync(python,['-B',`${root}/scripts/switch-runtime.py`,'--state',state,'--source',runtime,'--python',python],{stdio:'inherit'})
  for(const pid of running)try{process.kill(pid,'SIGTERM')}catch(error){if(error.code!=='ESRCH')console.warn('Close and reopen SessionGraph to load the installed UI')}
  if(running.length){
    const alive=pid=>{try{process.kill(pid,0);return true}catch{return false}}
    const deadline=Date.now()+5000
    while(running.some(alive)&&Date.now()<deadline)Atomics.wait(new Int32Array(new SharedArrayBuffer(4)),0,0,100)
    if(running.some(alive))throw new Error('Old app did not exit; restoring previous installation')
    try{execFileSync('open',[target])}catch{console.warn('Open /Applications/SessionGraph.app to load the installed UI')}
  }
  console.log(`Installed ${target}`)
} catch(error) {
  fs.rmSync(stage,{recursive:true,force:true})
  if(replaced&&fs.existsSync(backup)) {fs.rmSync(target,{recursive:true,force:true});fs.renameSync(backup,target)}
  throw error
}

// The previous bundle exists only during installation for failure recovery.
// Successful installs retain no automatic app backups. Cleanup failures are
// reported without rolling back a successfully installed app.
try {
  const folder=path.dirname(target), prefix=`${path.basename(target)}.backup-`
  for(const entry of fs.readdirSync(folder,{withFileTypes:true})) {
    const candidate=path.join(folder,entry.name)
    if(entry.isDirectory()&&entry.name.startsWith(prefix)&&/^\d+$/.test(entry.name.slice(prefix.length)))
      try{fs.rmSync(candidate,{recursive:true})}catch(error){console.warn(`Backup cleanup failed: ${candidate}: ${error.message}`)}
  }
} catch(error) {console.warn(`Backup cleanup failed: ${error.message}`)}
