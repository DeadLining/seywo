#!/usr/bin/env python3
"""Build a relocatable marketplace archive from an explicit runtime allowlist."""
import argparse
import json
from pathlib import Path
import shutil
import stat
import zipfile

REPO = Path(__file__).resolve().parents[1]


def build(output):
    plugin = REPO / 'seywo'
    version = json.loads((plugin / '.codex-plugin/plugin.json').read_text())['version']
    package = output / ('seywo-plugin-' + version)
    if package.exists():
        raise ValueError('Output already exists; choose another output directory.')
    package.mkdir(parents=True)
    marketplace = package / '.agents/plugins'
    marketplace.mkdir(parents=True)
    shutil.copy2(REPO / '.agents/plugins/marketplace.json', marketplace / 'marketplace.json')
    roots = ['.codex-plugin', 'bin', 'src', 'skills', 'docs']
    files = ['README.md', 'pyproject.toml', 'mcp.json', 'hooks.json',
             'ui/panel.html', 'ui/THIRD_PARTY_NOTICES.txt']
    sources = [plugin / name for name in files]
    public_contract = REPO / 'docs/seywo-mcp.md'
    if public_contract.is_file():
        sources.append(public_contract)
    for folder in roots:
        sources.extend(path for path in (plugin / folder).rglob('*')
                       if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc')
    for source in sorted(sources):
        if source.is_symlink():
            raise ValueError('Package sources must not be symlinks')
        relative = source.relative_to(plugin) if source.is_relative_to(plugin) else Path('docs') / source.name
        destination = package / 'seywo' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    for name in ('LICENSE', 'LICENSE.md'):
        if (REPO / name).is_file():
            shutil.copy2(REPO / name, package / name)
    (package / 'README.md').write_text('''# Seywo Codex plugin

Unzip this package, then add this directory as a local marketplace in Codex:

```sh
codex plugin marketplace add /absolute/path/to/seywo-plugin-VERSION
codex plugin add seywo@seywo-local
```

Enable Seywo in the current project and review/trust its four Hooks in Codex.
Default extraction reuses the installed Codex login and global model configuration.
No standalone Seywo/SessionGraph application or extra API key is required.
macOS/Linux needs Python 3.11+ or uv. First SDK provisioning requires network access.
Existing chat MCP connections may need to reconnect after installing/updating.
See seywo/docs/PLUGIN.md for settings, storage and verification boundaries.
'''.replace('VERSION', version))
    archive = output / (package.name + '.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                bundle.write(path, Path(package.name) / path.relative_to(package))
    return {'version': version, 'directory': str(package), 'archive': str(archive),
            'bytes': archive.stat().st_size, 'file_count': sum(p.is_file() for p in package.rglob('*')),
            'executable_entrypoint': bool((package / 'seywo/bin/seywo-runtime').stat().st_mode & stat.S_IXUSR)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPO / 'dist')
    args = parser.parse_args()
    print(json.dumps(build(args.output.resolve()), ensure_ascii=False, indent=2))
