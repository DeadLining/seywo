"""Validate conversation-derived task nodes before publishing them."""
from __future__ import annotations

import re
import hashlib
import json
import uuid
from datetime import datetime, timezone


NODE_KINDS = frozenset({'goal', 'problem', 'option', 'decision', 'action', 'result', 'next_step'})


def validate_extraction(payload: dict, entries: list[dict]) -> list[dict]:
    """Accept only source-backed claims, in source order, without guessing links."""
    if not isinstance(payload, dict) or not isinstance(payload.get('tasks'), list):
        raise ValueError('invalid_task_response')
    if len(payload['tasks']) > 100:
        raise ValueError('too_many_tasks')
    sources = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not isinstance(entry.get('text'), str):
            raise ValueError('invalid_source')
        if entry['id'] in sources:
            raise ValueError('duplicate_source')
        sources[entry['id']] = (index, entry)
    result = []
    for task in payload['tasks']:
        if not isinstance(task, dict) or not isinstance(task.get('title'), str):
            raise ValueError('invalid_task')
        title = task['title'].strip()
        nodes = task.get('nodes')
        if not title or len(title) > 120 or not isinstance(nodes, list) or not nodes or len(nodes) > 200:
            raise ValueError('invalid_task')
        accepted = []
        for node in nodes:
            if not isinstance(node, dict) or node.get('kind') not in NODE_KINDS:
                raise ValueError('invalid_node')
            label, entry_id, quote = node.get('label'), node.get('entry_id'), node.get('quote')
            if (not isinstance(label, str) or not label.strip() or len(label) > 200
                    or not isinstance(entry_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', entry_id)
                    or not isinstance(quote, str) or not quote.strip() or len(quote) > 2000
                    or entry_id not in sources or quote not in sources[entry_id][1]['text']):
                raise ValueError('invalid_node_evidence')
            index, entry = sources[entry_id]
            if entry.get('context_only'):
                raise ValueError('context_only_evidence')
            if node['kind'] == 'decision' and entry.get('role') != 'user':
                raise ValueError('decision_requires_user_source')
            parent = node.get('parent_entry_id') or ''
            branch_quote = node.get('branch_quote') or ''
            if parent and (not isinstance(parent, str) or parent not in sources or parent == entry_id
                    or not isinstance(branch_quote, str) or not branch_quote.strip() or len(branch_quote) > 2000
                    or branch_quote not in entry['text'] and branch_quote not in sources[parent][1]['text']):
                raise ValueError('invalid_branch_evidence')
            accepted.append((index, {'kind': node['kind'], 'label': label.strip(),
                                     'entry_id': entry_id, 'quote': quote,
                                     'timestamp': entry.get('timestamp', ''), 'parent_entry_id': parent,
                                     'branch_quote': branch_quote, 'source_rowid': entry.get('source_rowid', index)}))
        accepted.sort(key=lambda item: item[0])
        positions = [node['entry_id'] for _, node in accepted]
        for position, (_, node) in enumerate(accepted):
            if node['parent_entry_id'] and (positions.count(node['parent_entry_id']) != 1
                    or positions.index(node['parent_entry_id']) >= position):
                raise ValueError('invalid_branch_evidence')
        result.append({'title': title, 'nodes': [node for _, node in accepted]})
    return result


def save_extraction(db, project_id: str, chat_id: str, tasks: list[dict], through_rowid: int | None = None,
                    *, fragment: dict | None = None, clear_fragment: bool = False) -> int:
    """Publish validated results atomically; repeated exact nodes are idempotent."""
    with db:
        db.execute('''CREATE TABLE IF NOT EXISTS flow_tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS flow_nodes (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL, chat_id TEXT NOT NULL,
            kind TEXT NOT NULL, label TEXT NOT NULL, entry_id TEXT NOT NULL,
            quote TEXT NOT NULL, source_time TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES flow_tasks(id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS flow_progress (
            project_id TEXT NOT NULL, chat_id TEXT NOT NULL, last_rowid INTEGER NOT NULL,
            PRIMARY KEY(project_id, chat_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS flow_fragment_progress (
            project_id TEXT NOT NULL, chat_id TEXT NOT NULL, entry_id TEXT NOT NULL,
            source_hash TEXT NOT NULL, next_start INTEGER NOT NULL,
            PRIMARY KEY(project_id, chat_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS flow_task_aliases (
            id TEXT PRIMARY KEY, target_id TEXT NOT NULL)''')
        columns = {row[1] for row in db.execute('PRAGMA table_info(flow_nodes)')}
        for name in ('hidden', 'manual', 'edited', 'source_rowid'):
            if name not in columns:
                db.execute(f'ALTER TABLE flow_nodes ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
        for name in ('parent_entry_id', 'branch_quote'):
            if name not in columns:
                db.execute(f"ALTER TABLE flow_nodes ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        saved = 0
        for task in tasks:
            # Only an exact task title can match automatically; it never implies causality.
            source_task_id = hashlib.sha256(json.dumps([project_id, task['title']], ensure_ascii=False).encode()).hexdigest()
            matches = db.execute('SELECT id FROM flow_tasks WHERE project_id=? AND title=? LIMIT 2',
                                 (project_id, task['title'])).fetchall()
            task_id = matches[0][0] if len(matches) == 1 else source_task_id
            alias = db.execute('SELECT target_id FROM flow_task_aliases WHERE id=?', (source_task_id,)).fetchone()
            if alias and not matches:
                task_id = alias[0]
            db.execute('INSERT OR IGNORE INTO flow_tasks(id,project_id,title) VALUES(?,?,?)',
                       (task_id, project_id, task['title']))
            for node in task['nodes']:
                identity = [source_task_id, chat_id, node['entry_id'], node['kind'], node['quote']]
                node_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
                saved += db.execute('''INSERT OR IGNORE INTO flow_nodes
                    (id,task_id,chat_id,kind,label,entry_id,quote,source_time,parent_entry_id,branch_quote,source_rowid)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)''', (node_id, task_id, chat_id, node['kind'],
                    node['label'], node['entry_id'], node['quote'], node['timestamp'],
                    node.get('parent_entry_id', ''), node.get('branch_quote', ''), node.get('source_rowid', 0))).rowcount
        if through_rowid is not None:
            db.execute('''INSERT INTO flow_progress(project_id,chat_id,last_rowid) VALUES(?,?,?)
                ON CONFLICT(project_id,chat_id) DO UPDATE SET last_rowid=MAX(last_rowid,excluded.last_rowid)''',
                       (project_id, chat_id, through_rowid))
        if fragment is not None:
            db.execute('''INSERT INTO flow_fragment_progress(project_id,chat_id,entry_id,source_hash,next_start)
                VALUES(?,?,?,?,?) ON CONFLICT(project_id,chat_id) DO UPDATE SET
                entry_id=excluded.entry_id,source_hash=excluded.source_hash,next_start=excluded.next_start''',
                (project_id, chat_id, fragment['entry_id'], fragment['source_hash'], fragment['next_start']))
        if clear_fragment:
            db.execute('DELETE FROM flow_fragment_progress WHERE project_id=? AND chat_id=?', (project_id, chat_id))
    return saved


def edit_node(db, project_id: str, action: str, task_id: str, node_id: str = '', label: str = ''):
    if action not in {'add', 'edit', 'delete'}:
        raise ValueError('invalid_action')
    if not isinstance(task_id, str) or not re.fullmatch('[0-9a-f]{64}', task_id):
        raise ValueError('invalid_task')
    if action != 'add' and (not isinstance(node_id, str) or not re.fullmatch('[0-9a-f]{64}', node_id)):
        raise ValueError('invalid_node')
    if action != 'delete' and (not isinstance(label, str) or not label.strip() or len(label) > 200):
        raise ValueError('invalid_label')
    with db:
        if not db.execute('SELECT 1 FROM flow_tasks WHERE id=? AND project_id=?', (task_id, project_id)).fetchone():
            raise ValueError('invalid_task')
        if action == 'add':
            node_id = uuid.uuid4().hex + uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            db.execute('''INSERT INTO flow_nodes(id,task_id,chat_id,kind,label,entry_id,quote,source_time,manual)
                VALUES(?,?,?,?,?,?,?,?,1)''', (node_id, task_id, '', 'action', label.strip(), '', '', now))
        elif action == 'edit':
            changed = db.execute('UPDATE flow_nodes SET label=?,edited=1 WHERE id=? AND task_id=? AND hidden=0',
                                 (label.strip(), node_id, task_id)).rowcount
            if not changed:
                raise ValueError('invalid_node')
        else:
            changed = db.execute('UPDATE flow_nodes SET hidden=1 WHERE id=? AND task_id=? AND hidden=0',
                                 (node_id, task_id)).rowcount
            if not changed:
                raise ValueError('invalid_node')
    return node_id


def edit_task(db, project_id: str, action: str, task_id: str, title: str = '', target: str = '', node: str = ''):
    if action not in {'rename', 'merge', 'split'} or not re.fullmatch('[0-9a-f]{64}', task_id or ''):
        raise ValueError('invalid_task')
    with db:
        if not db.execute('SELECT 1 FROM flow_tasks WHERE id=? AND project_id=?', (task_id, project_id)).fetchone():
            raise ValueError('invalid_task')
        if action in {'rename', 'split'} and (not isinstance(title, str) or not title.strip() or len(title) > 120):
            raise ValueError('invalid_title')
        if action == 'rename':
            db.execute('UPDATE flow_tasks SET title=? WHERE id=?', (title.strip(), task_id))
        elif action == 'merge':
            if not re.fullmatch('[0-9a-f]{64}', target or '') or target == task_id or not db.execute(
                    'SELECT 1 FROM flow_tasks WHERE id=? AND project_id=?', (target, project_id)).fetchone():
                raise ValueError('invalid_target')
            db.execute('UPDATE flow_nodes SET task_id=? WHERE task_id=?', (target, task_id))
            db.execute('UPDATE flow_task_aliases SET target_id=? WHERE target_id=?', (target, task_id))
            db.execute('INSERT OR REPLACE INTO flow_task_aliases(id,target_id) VALUES(?,?)', (task_id, target))
            db.execute('DELETE FROM flow_tasks WHERE id=?', (task_id,))
        else:
            if not re.fullmatch('[0-9a-f]{64}', node or '') or not db.execute(
                    'SELECT 1 FROM flow_nodes WHERE id=? AND task_id=? AND hidden=0', (node, task_id)).fetchone():
                raise ValueError('invalid_node')
            new_task = uuid.uuid4().hex + uuid.uuid4().hex
            db.execute('INSERT INTO flow_tasks(id,project_id,title) VALUES(?,?,?)', (new_task, project_id, title.strip()))
            db.execute('UPDATE flow_nodes SET task_id=? WHERE id=?', (new_task, node))
