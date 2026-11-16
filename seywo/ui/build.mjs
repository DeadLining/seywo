import { createRequire } from 'node:module';
const require = createRequire(new URL('../../tools/seywo-ui/package.json', import.meta.url));
const { build } = require('esbuild');
import { readFile, writeFile } from 'node:fs/promises';
const result = await build({ entryPoints: [new URL('./entry.js', import.meta.url).pathname], bundle: true, nodePaths: [new URL('../../tools/seywo-ui/node_modules', import.meta.url).pathname], write: false, minify: true, format: 'esm', target: 'es2022', legalComments: 'eof' });
const shell = await readFile(new URL('./shell.html', import.meta.url), 'utf8');
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
await writeFile(new URL('./panel.html', import.meta.url), shell.replace('/*SEYWO_BUNDLE*/', () => js));
console.log(`Packaged Seywo UI: ${Buffer.byteLength(js)} JavaScript bytes`);
const licenses = ['@modelcontextprotocol/ext-apps', '@modelcontextprotocol/client', '@modelcontextprotocol/core', 'zod'];
let notices = 'Seywo UI third-party notices\n\nThe bundled MCP Apps SDK includes the following MIT-licensed packages.\n';
for (const name of licenses) {
    const dir = new URL(`../../tools/seywo-ui/node_modules/${name}/`, import.meta.url);
    const pkg = JSON.parse(await readFile(new URL('package.json', dir), 'utf8'));
    notices += `\n--- ${name} ${pkg.version} ---\n\n` + await readFile(new URL('LICENSE', dir), 'utf8');
}
await writeFile(new URL('./THIRD_PARTY_NOTICES.txt', import.meta.url), notices);
