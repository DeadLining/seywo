"""Opt-in global lifecycle routing. No network or extraction during initialization."""
from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path

from .core import Store, digest, load_config

EVENTS = ('SessionStart', 'UserPromptSubmit', 'PreCompact', 'PostCompact', 'Stop', 'SessionEnd')


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.sg-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def project_root(cwd: str) -> Path | None:
    if not isinstance(cwd, str) or not cwd or not Path(cwd).is_absolute():
        return None
    current = Path(cwd).resolve()
    if not current.is_dir() or current in (Path('/'), Path.home()):
        return None
    for candidate in (current, *current.parents):
        if candidate in (Path('/'), Path.home()):
            break
        state = candidate / '.sessiongraph'
        if (state / 'disabled').exists() or (state / 'config.json').exists():
            return candidate
        if (candidate / '.git').exists() or (candidate / '.codex').is_dir():
            return candidate
    return current


def register_project(registry: Path, root: Path) -> None:
    # Readers see either full version; concurrent projects cannot lose each other.
    with locked(registry.with_suffix('.lock')):
        data = json.loads(registry.read_text()) if registry.exists() else {'projects': []}
        entries = data.get('projects')
        if not isinstance(entries, list) or any(
            not isinstance(p, dict) or not isinstance(p.get('root'), str) for p in entries
        ):
            raise ValueError('invalid project registry')
        if any(Path(p['root']).resolve() == root for p in entries):
            return
        if len(entries) >= 100:
            raise ValueError('project registry limit reached')
        entries.append({'root': str(root), 'name': root.name})
        atomic_write(registry, json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def graph_template(config):
    from .pipeline_settings import graph_template as selected
    return selected(config)


def ensure_project(cwd: str, settings: dict) -> Path | None:
    if settings.get('enabled') is not True:
        return None
    root = project_root(cwd)
    if root is None:
        return None
    state = root / '.sessiongraph'
    # Do not create a database through a symlink to another project's state.
    if state.is_symlink() or (state / 'disabled').exists():
        return None
    state.mkdir(mode=0o700, exist_ok=True)
    config = state / 'config.json'
    with locked(state / 'init.lock'):
        if not config.exists():
            template = json.loads(Path(settings['template_config']).read_text())
            since = settings['enabled_since']
            if not isinstance(since, str) or not re.fullmatch(
                r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', since
            ):
                raise ValueError('invalid global enable timestamp')
            # Copy only provider options, never literal credentials or source roots.
            fields = ('base_url_env', 'api_key_env', 'model_env', 'timeout_seconds',
                      'max_output_tokens', 'protocol', 'endpoint', 'model', 'auth')
            provider = {k: v for k, v in template['provider'].items() if k in fields}
            # Publish config last: a crash cannot leave a usable default config
            # lacking the enable cutoff / pointing at an unintended provider.
            raw = {
                'project_id': digest(str(root))[:20], 'roots': [str(root)],
                'database': 'index.sqlite3', 'policy': 'policy.json',
                'provider': provider, 'extraction': template['extraction'],
                'hook': {'allowed_transcript_roots': settings['transcript_roots'],
                         'automatic_since': since, 'enabled': True},
            }
            graph = graph_template(template)
            if graph is not None:
                raw['graph_v2'] = graph
            atomic_write(state / 'policy.json', Path(__file__).with_name('policy.json').read_text())
            atomic_write(state / '.gitignore', '*\n!.gitignore\n')
            atomic_write(config, json.dumps(raw, ensure_ascii=False, indent=2) + '\n')
        current = json.loads(config.read_text())
        template = json.loads(Path(settings['template_config']).read_text())
        graph = graph_template(template)
        if 'graph_v2' not in current and graph is not None:
            current['graph_v2'] = graph
            atomic_write(config, json.dumps(current, ensure_ascii=False, indent=2) + '\n')
        cfg = load_config(config)
        if cfg.get('hook', {}).get('enabled') is False:
            return None
        if cfg['roots'] != [str(root)]:
            raise ValueError('project roots mismatch')
        if not Path(cfg['database']).exists():
            Store(cfg).close()
    register_project(Path(settings['registry']), root)
    return config


def merge_hooks(original: dict, command: str) -> dict:
    data = copy.deepcopy(original)
    hooks = data.setdefault('hooks', {})
    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        found = any(h.get('command') == command for group in groups
                    for h in group.get('hooks', []))
        if not found:
            groups.append({'hooks': [{'type': 'command', 'command': command}]})
    return data


def remove_local_hooks(text: str, command: str) -> str:
    """Remove only stand-alone SG groups; refuse mixed groups instead of losing data."""
    original = tomllib.loads(text)
    expected = copy.deepcopy(original)
    for event, groups in list(expected.get('hooks', {}).items()):
        kept = []
        for group in groups:
            members = group.get('hooks', [])
            matching = [h for h in members if h.get('command') == command]
            if matching:
                if len(matching) != len(members):
                    raise ValueError('mixed local hook group requires manual migration')
            else:
                kept.append(group)
        if kept:
            expected['hooks'][event] = kept
        else:
            del expected['hooks'][event]
    if not expected.get('hooks'):
        expected.pop('hooks', None)
    # A group's range ends at the first header that is not its own child table.
    headers = list(re.finditer(r'(?m)^\[.+\][ \t]*$', text))
    cuts = []
    for i, match in enumerate(headers):
        group = re.fullmatch(r'\[\[hooks\.(\w+)\]\]', match.group().strip())
        if not group:
            continue
        end = len(text)
        for following in headers[i + 1:]:
            if not following.group().strip().startswith('[[hooks.' + group[1] + '.'):
                end = following.start()
                break
        block = text[match.start():end]
        parsed = tomllib.loads(block)['hooks'][group[1]][0]
        if any(h.get('command') == command for h in parsed.get('hooks', [])):
            cuts.append((match.start(), end))
    for start, end in reversed(cuts):
        text = text[:start] + text[end:]
    if tomllib.loads(text) != expected:
        raise ValueError('local migration validation failed')
    return text


def install(codex_home: Path, local_config: Path, command: str, settings: dict) -> dict:
    """Backed-up, idempotent registration. Never edits Codex trust records."""
    from datetime import datetime, timezone
    import uuid

    with locked(codex_home / 'sessiongraph-install.lock'):
        hooks_path = codex_home / 'hooks.json'
        settings_path = codex_home / 'sessiongraph.json'
        hooks = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
        merged = merge_hooks(hooks, command)
        local = local_config.read_text()
        migrated = remove_local_hooks(local, command)
        if settings_path.exists():
            prior = json.loads(settings_path.read_text())
            settings = {**settings, 'enabled_since': prior['enabled_since']}
            if 'cleanup_missing_chats' not in settings and 'cleanup_missing_chats' in prior:
                settings['cleanup_missing_chats'] = prior['cleanup_missing_chats']
        planned = {
            settings_path: json.dumps(settings, ensure_ascii=False, indent=2) + '\n',
            hooks_path: json.dumps(merged, ensure_ascii=False, indent=2) + '\n',
            local_config: migrated,
        }
        changed, backups = [], []
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        originals = {p: p.read_bytes() if p.exists() else None for p in planned}
        for path, content in planned.items():
            original = originals[path]
            if original == content.encode():
                continue
            # Refuse a symlink rather than accidentally breaking a managed setup.
            if path.is_symlink():
                raise ValueError('refusing to replace symlink configuration')
            if original is not None:
                backup = path.with_name(path.name + '.sessiongraph-backup-' + stamp)
                with backup.open('xb') as out:
                    out.write(original)
                backup.chmod(0o600)
                backups.append(str(backup))
        try:
            for path, content in planned.items():
                if originals[path] == content.encode():
                    continue
                if (path.read_bytes() if path.exists() else None) != originals[path]:
                    raise ValueError('configuration changed during installation')
                atomic_write(path, content)
                changed.append(str(path))
        except Exception:
            # Roll back only files this installer changed, not a concurrent edit.
            for name in reversed(changed):
                path = Path(name)
                if path.read_text() == planned[path]:
                    if originals[path] is None:
                        path.unlink()
                    else:
                        atomic_write(path, originals[path].decode())
            raise
        return {'changed': changed, 'backups': backups, 'events': list(EVENTS),
                'trusted': False, 'note': 'Registration only; review hooks in Codex.'}
