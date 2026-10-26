"""Exercise packaged CLI/native/Hook reads on this project's isolated snapshot."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    production = (args.config.parent / config['database']).resolve()
    snapshot = args.snapshot.resolve()
    if not snapshot.is_file() or snapshot == production or snapshot.is_relative_to(production.parent):
        raise ValueError('isolated_snapshot_required')
    if args.output.exists():
        raise ValueError('report_already_exists')
    with tempfile.TemporaryDirectory(prefix='sg-packaged-canary-') as folder:
        root = Path(folder).resolve()
        state = root / '.sessiongraph'
        state.mkdir()
        config.update(database=str(snapshot), roots=[str(root)])
        config['graph_v2']['revision_policy'] = dict(enabled=True, mode='effective')
        policy = (args.config.parent / config['policy']).resolve()
        config['policy'] = str(policy)
        cfg = state / 'config.json'
        cfg.write_text(json.dumps(config))
        (root / 'projects.json').write_text(json.dumps(dict(projects=[dict(root=str(root), name='Fruit-fly isolated canary')])))
        env = {**os.environ, 'SESSIONGRAPH_RUNTIME_HOME':str(root), 'CODEX_HOME':str(root / 'codex'),
               'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONPATH':str(args.runtime)}
        entrypoint = args.runtime / 'entrypoint.py'
        calls = []
        def call(command, payload=None):
            result = subprocess.run([sys.executable, '-B', str(entrypoint), *command],
                input=json.dumps(payload) if payload else None, text=True, capture_output=True, env=env,
                cwd=root, timeout=30, check=True)
            parsed = json.loads(result.stdout)
            calls.append(dict(command=command[0], output_bytes=len(result.stdout.encode())))
            return parsed
        db = sqlite3.connect(snapshot.as_uri() + '?mode=ro', uri=True)
        try:
            version = db.execute('PRAGMA data_version').fetchone()[0]
            node, content = db.execute('SELECT id,content FROM work_memory WHERE id IN (SELECT node FROM sg_revision_items) ORDER BY id LIMIT 1').fetchone()
            runtime = call(['runtime-status'])
            assert Path(runtime['source']).resolve() == args.runtime.resolve()
            assert runtime['model_calls'] == 0
            native = call(['native-read'], dict(operation='memory', project=hashlib.sha256(str(root).encode()).hexdigest()[:20], id=node))
            assert native['memory']['content'] == content
            assert native['graph_v2']['validity']['state'] == 'legacy_unassessed'
            resume = call(['--config',str(cfg),'resume','--limit','3'])
            assert resume['memories']
            expanded = call(['--config',str(cfg),'expand','e_5b307ba5b00f8bf33e0bea026c5a5b34','--radius','0'])
            assert 'max-token=1' in json.dumps(expanded, ensure_ascii=False)
            hook = call(['hook-event'],dict(hook_event_name='SessionStart',session_id='isolated-canary',cwd=str(root)))
            context = hook['hookSpecificOutput']['additionalContext']
            assert 'validity=legacy_unassessed' in context
            assert db.execute('PRAGMA data_version').fetchone()[0] == version
        finally:
            db.close()
    result = dict(runtime=runtime, operations=calls, native_old_id_preserved=True,
        source_expansion_verified=True, hook_context_validity_preserved=True, database_unchanged=True,
        production_writes=0, model_calls=0, installed_application_replaced=False,
        scope='isolated packaged Python runtime, not actual Codex host delivery or new frontend bundle')
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
