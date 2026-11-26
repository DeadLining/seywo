"""Preview and confirm initialization of explicitly named projects only."""
import json
from pathlib import Path

from .config import initialize, project_directory, state_dir
from .project_scope import ProjectBinding

FILES = ('.gitignore', 'config.json', 'index.sqlite3', '.init.lock', 'storage-migration.lock')


def _plan(root):
    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        raise ValueError('project root must be an explicit absolute path')
    root = project_directory(candidate, strict=True)
    if not root.is_dir():
        raise ValueError('project root must be an existing directory')
    state = state_dir(root)
    if state.exists() and not state.is_dir():
        raise ValueError('.seywo must be a directory')
    config = state / 'config.json'
    if config.is_symlink():
        raise ValueError('Seywo configuration must not be a symlink')
    base = {'project_root': str(root), 'create': [], 'modify': [], 'directories': []}
    # Skip even invalid configurations: this command does not repair or enable
    # existing projects and must not read/reset their settings or databases.
    if config.exists():
        return {**base, 'status': 'skipped', 'reason': 'config.json already exists'}
    ProjectBinding.at(root)
    for name in FILES:
        path = state / name
        if path.is_symlink():
            raise ValueError(f'.seywo/{name} must not be a symlink')
        if path.exists() and not path.is_file():
            raise ValueError(f'.seywo/{name} must be a regular file')
    if (state / 'index.sqlite3').exists():
        raise ValueError('index.sqlite3 already exists without config.json; restore the project configuration')
    return {**base, 'status': 'ready',
            'directories': [] if state.exists() else [str(state)],
            'create': [str(state / name) for name in FILES if not (state / name).exists()]}


def _error(root, exc):
    return {'project_root': str(root), 'status': 'failed', 'error': str(exc),
            'create': [], 'modify': [], 'directories': []}


def _emit(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def run(roots, *, dry_run=False):
    plans, seen = [], set()
    for root in roots:
        try:
            plan = _plan(root)
        except Exception as exc:
            plan = _error(root, exc)
        key = plan['project_root']
        if key not in seen:
            seen.add(key)
            plans.append(plan)
    _emit({'phase': 'plan', 'projects': plans, 'model_calls': 0,
           'note': '清单包含最终文件和锁文件；执行期间可能短暂产生原子写入临时文件和 SQLite journal。'})
    failed = any(plan['status'] == 'failed' for plan in plans)
    if dry_run or not any(plan['status'] == 'ready' for plan in plans):
        return int(failed)
    try:
        confirmed = input('输入 yes 确认逐个初始化以上项目，其他输入取消：').strip().lower() == 'yes'
    except (EOFError, KeyboardInterrupt):
        confirmed = False
    print()
    if not confirmed:
        _emit({'phase': 'result', 'cancelled': True, 'model_calls': 0})
        return int(failed)

    results = []
    for plan in plans:
        if plan['status'] != 'ready':
            results.append(plan)
            continue
        root = plan['project_root']
        try:
            current = _plan(root)
            if current['status'] == 'skipped':
                results.append(current)
                continue
            # Do not write a newly required file/directory absent from the
            # confirmed preview. Newly present safe files can be preserved.
            for kind in ('create', 'modify', 'directories'):
                if not set(current[kind]) <= set(plan[kind]):
                    raise ValueError('project changed after preview; rerun init-batch to review the new plan')
            result = initialize(Path(root), plugin_hook=True, skip_existing=True)
            results.append({'project_root': root, 'status': 'skipped' if result.get('skipped') else 'initialized',
                            **result})
        except Exception as exc:
            # Isolate each project, including unexpected initialization failures;
            # a failed root must never prevent later explicit roots from running.
            results.append(_error(root, exc))
    _emit({'phase': 'result', 'cancelled': False, 'projects': results, 'model_calls': 0})
    return int(any(item['status'] == 'failed' for item in results))
