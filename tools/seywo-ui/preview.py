#!/usr/bin/env python3
"""Development-only local read-only MCP Apps preview; never shipped in the plugin."""
import argparse
import asyncio
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'seywo/src'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    # Never initialize a project as a side effect of previewing it.
    project = args.project.resolve()
    if not (project / '.seywo/config.json').is_file():
        parser.error('Preview requires an existing initialized Seywo project')
    from seywo.plugin import create_plugin_server
    plugin = create_plugin_server(project)
    host_js = subprocess.check_output(
        ['node', str(ROOT / 'tools/seywo-ui/node_modules/esbuild/bin/esbuild'),
         str(ROOT / 'tools/seywo-ui/preview-host.js'), '--bundle', '--format=esm', '--minify'],
        cwd=ROOT,
    )
    shell = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Seywo MCP Apps Preview</title>
<style>body{margin:0;background:#e9edea;font:13px system-ui;color:#334b44}header{padding:12px 24px;display:flex;gap:16px;align-items:center;border-bottom:1px solid #cbd5d0;background:white}header span{flex:1}iframe{display:block;border:0;background:#f8faf8;height:calc(100vh - 58px);max-width:100%;margin:auto;box-shadow:0 0 1px #9fa}select{padding:4px;border:1px solid #cad5d0;border-radius:5px}</style>
<header><strong>Seywo</strong><span data-status>正在连接 MCP Apps...</span><label>宽度 <select data-width><option>680px</option><option>360px</option></select></label><label>主题 <select data-theme><option value="light">浅色</option><option value="dark">深色</option></select></label></header>
<iframe title="Seywo" style="width:680px" sandbox="allow-scripts allow-same-origin"></iframe><script type="module" src="/host.js"></script></html>'''.encode()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, body, mime, status=200):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            routes = {'/': (shell, 'text/html; charset=utf-8'),
                      '/host.js': (host_js, 'text/javascript; charset=utf-8')}
            if self.path == '/panel':
                return self.reply((ROOT / 'seywo/ui/panel.html').read_bytes(), 'text/html; charset=utf-8')
            body, mime = routes.get(self.path, (b'Not found', 'text/plain'))
            self.reply(body, mime, 200 if self.path in routes else 404)

        def do_POST(self):
            expected = f'http://127.0.0.1:{self.server.server_port}'
            if (self.path != '/call' or self.headers.get('Origin') != expected
                    or self.headers.get('Content-Type') != 'application/json'):
                return self.reply(b'Forbidden', 'text/plain', 403)
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size < 16384:
                return self.reply(b'Invalid request', 'text/plain', 400)
            try:
                data = json.loads(self.rfile.read(size))
                result = asyncio.run(plugin.call_tool('seywo_panel_data', data))
                body = {'content': [item.model_dump(by_alias=True, exclude_none=True) for item in result]}
                self.reply(json.dumps(body, ensure_ascii=False).encode(), 'application/json')
            except Exception:
                self.reply(b'{"isError":true,"content":[]}', 'application/json')

        def log_message(self, *_):
            pass

    with HTTPServer(('127.0.0.1', args.port), Handler) as server:
        print(f'Seywo read-only UI preview: http://127.0.0.1:{server.server_port}/', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
