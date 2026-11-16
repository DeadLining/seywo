import { execFileSync } from 'node:child_process'
import path from 'node:path'
const root=path.resolve(import.meta.dirname,'..')
execFileSync(process.execPath,[path.join(root,'scripts/scaffold-resources.mjs')],{cwd:root,stdio:'inherit'})
if(process.platform==='win32') {
  execFileSync(process.env.ComSpec||'cmd.exe',['/d','/s','/c','npm exec -- tauri build --bundles nsis'],{cwd:root,stdio:'inherit'})
  process.exit(0)
}
execFileSync('npm',['exec','--','tauri','build','--bundles','app'],{cwd:root,stdio:'inherit'})
const bundle=path.join(root,'src-tauri/target/release/bundle/macos/Seywo.app')
const identities=execFileSync('security',['find-identity','-v','-p','codesigning'],{encoding:'utf8'})
const identity=process.env.SESSIONGRAPH_SIGNING_IDENTITY||(identities.match(/([A-F0-9]{40}) "OpenHuman Dev Signer"/)?.[1])||'-'
execFileSync('codesign',['--force','--sign',identity,'--timestamp=none',bundle],{stdio:'inherit'})
execFileSync('codesign',['--verify','--deep','--strict',bundle],{stdio:'inherit'})
