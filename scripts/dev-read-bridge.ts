import type { Plugin } from 'vite'
import { execFile } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import crypto from 'node:crypto'

// Opt-in, loopback-only, read-only development bridge for one explicit project.
export function devReadBridge(): Plugin {
  let temporary = ''
  const config = process.env.SESSIONGRAPH_DEV_CONFIG
  const root = config ? path.resolve(config, '../..') : ''
  const project = root ? crypto.createHash('sha256').update(root).digest('hex').slice(0,20) : ''
  let slots = 0
  return {
    name:'sessiongraph-local-read',
    configureServer(server) {
      if (!config || !fs.existsSync(config)) return
      temporary=fs.mkdtempSync(path.join(os.tmpdir(),'sg-dev-read-'))
      fs.writeFileSync(path.join(temporary,'projects.json'),JSON.stringify({projects:[{root,name:path.basename(root)}]}))
      server.httpServer?.once('close',()=>fs.rmSync(temporary,{recursive:true,force:true}))
      server.middlewares.use('/__sessiongraph/read',(req,res)=>{
        if(req.method!=='POST'||!['127.0.0.1','::1','::ffff:127.0.0.1'].includes(req.socket.remoteAddress||'')||slots>=4){res.statusCode=403;res.end();return}
        const origin=req.headers.origin
        if(origin && origin!==`http://${req.headers.host}`){res.statusCode=403;res.end();return}
        let body=''
        req.on('data',chunk=>{body+=chunk;if(body.length>4096)req.destroy()})
        req.on('end',()=>{
          let request:Record<string,unknown>
          try { request=JSON.parse(body);if(!['projects','work_map','status','memory','evidence','project_chats','flow_tasks'].includes(String(request.operation))||(request.operation!=='projects'&&request.project!==project))throw Error() }
          catch {res.statusCode=400;res.end();return}
          slots++
          const child=execFile(process.env.SESSIONGRAPH_PYTHON||'/Library/Frameworks/Python.framework/Versions/3.12/bin/python3',['-B',path.resolve('runtime/entrypoint.py'),'native-read'],{env:{...process.env,SESSIONGRAPH_RUNTIME_HOME:temporary},timeout:10000,maxBuffer:8*1024*1024},(error,stdout)=>{
            slots--;res.setHeader('Content-Type','application/json');res.setHeader('Cache-Control','no-store');res.statusCode=error?500:200;res.end(stdout||JSON.stringify({error:'local_read_failed',model_calls:0}))
          })
          child.stdin?.end(JSON.stringify(request))
        })
      })
    },
  }
}
