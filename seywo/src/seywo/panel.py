"""Read-only, paged projections for the plugin panel; no model execution."""
import json

from .narrative import Narrative, empty
from .retrieval import Retrieval, identity, integer, page


def short_node(item, chat_id, version):
    if item is None:
        return None
    return {key: item.get(key) for key in ('id', 'title', 'type', 'status', 'hidden')} | {
        'object_id': identity('n', chat_id, version, item['id']),
        'evidence_count': len(item.get('evidence', [])),
    }


def compare_documents(before, after, chat_id='', before_version=0, after_version=1):
    old = {i['id']: i for i in before['items'] if not i.get('hidden')}
    new = {i['id']: i for i in after['items'] if not i.get('hidden')}
    prior_merges = {json.dumps(i, sort_keys=True) for i in before.get('identity_changes', [])}
    merged = {item for change in after.get('identity_changes', [])
              if change.get('kind') == 'item_merge' and json.dumps(change, sort_keys=True) not in prior_merges
              for item in change['from_ids']}
    after_all = {i['id']: i for i in after['items']}
    old_locations = {p['item_id']: p for p in before['placements']}
    new_locations = {p['item_id']: p for p in after['placements']}
    counts = dict.fromkeys(('added', 'updated', 'completed', 'merged', 'hidden', 'removed'), 0)
    rows = []
    for item_id in dict.fromkeys([*old, *new]):
        previous, current = old.get(item_id), new.get(item_id)
        if previous is None:
            operation = 'added'
        elif current is None:
            operation = 'merged' if item_id in merged else 'hidden' if item_id in after_all else 'removed'
        elif previous != current or old_locations.get(item_id) != new_locations.get(item_id):
            operation = 'updated'
            if previous.get('status') != 'resolved' and current.get('status') == 'resolved':
                counts['completed'] += 1
        else:
            continue
        counts[operation] += 1
        rows.append({'item_id': item_id, 'operation': operation,
                     'before': short_node(previous, chat_id, before_version),
                     'after': short_node(current, chat_id, after_version)})
    return {'counts': counts, 'results': rows, 'before_count': len(old), 'after_count': len(new),
            'structure_changed': any(before.get(key) != after.get(key)
                                     for key in ('phases', 'branches', 'overview'))}


class Panel:
    def __init__(self, store):
        self.store, self.db = store, store.db
        self.reader = Retrieval(store)

    def chats(self, offset=0, limit=20):
        integer(offset, 'offset', 0, 10**9)
        integer(limit, 'limit', 1, 50)
        chats = self.reader.scope()
        rows = []
        # Hydrate only the requested page, never all chat histories.
        for chat in chats[offset:offset + limit + 1]:
            version, _ = Narrative(self.store)._head(chat)
            doc = Narrative(self.store)._body(chat, version)
            goals = set(doc['overview'].get('goal_item_ids', []))
            title = next((i.get('title', '') for i in doc['items'] if i['id'] in goals), '')
            rows.append({'chat_id': chat, 'title': title or '未命名对话', 'version': version,
                         'node_count': sum(not i.get('hidden') for i in doc['items'])})
        result = page(rows, limit=limit, max_bytes=32000)
        result['retrieval']['offset'] = offset
        if result['retrieval']['next_offset'] is not None:
            result['retrieval']['next_offset'] += offset
        result.update(project_root=self.store.config['project_root'], chat_count=len(chats))
        return result

    def mainline(self, chat_id):
        """Current Chat reading line. Stage records are a separate lazy projection."""
        self.store.require_human_chat(chat_id)
        service = Narrative(self.store)
        head, _ = service._head(chat_id)
        if not head:
            return {'chat_id': chat_id, 'version': 0, 'state': 'unpublished',
                    'phases': [], 'branches': [], 'goals': [], 'open_questions': [],
                    'next_steps': [], 'focus': {}, 'model_calls': 0}
        view = service.read(chat_id)
        doc = view['narrative']
        visible = {i['id']: i for i in doc['items'] if not i.get('hidden')}
        result = {'chat_id': chat_id, 'version': view['version'], 'state': view['state'],
                  'focus': doc['overview']['focus'], 'model_calls': 0,
                  'phases': [{k: p[k] for k in ('id', 'title', 'state') if k in p} for p in doc['phases']],
                  'branches': [{k: b[k] for k in ('id', 'phase_id', 'title', 'state') if k in b} for b in doc['branches']]}
        for name, key in (('goals', 'goal_item_ids'), ('open_questions', 'open_question_ids'),
                          ('next_steps', 'next_step_ids')):
            result[name] = [short_node(visible[i], chat_id, view['version'])
                            for i in doc['overview'][key] if i in visible]
        return result

    def narrative(self, chat_id, version=None, phase_id=None, branch_id=None, offset=0, limit=20):
        if version is not None:
            integer(version, 'version', 1, 10**9)
        view = Narrative(self.store).read(chat_id, version=version)
        doc = view['narrative']
        selected = phase_id if phase_id is not None else doc['overview']['focus']['phase_id']
        selected_branch = branch_id if branch_id is not None else (
            doc['overview']['focus']['branch_id'] if phase_id is None else None)
        if phase_id is not None and phase_id not in {p['id'] for p in doc['phases']}:
            raise ValueError('unknown_phase')
        if selected_branch is not None and not any(b['id'] == selected_branch and b['phase_id'] == selected for b in doc['branches']):
            raise ValueError('unknown_branch')
        locations = {p['item_id']: p for p in doc['placements']}
        visible = [i for i in doc['items'] if not i.get('hidden')]
        by_id = {i['id']: i for i in visible}
        nodes = [short_node(i, chat_id, view['version']) for i in visible
                 if locations[i['id']]['phase_id'] == selected and locations[i['id']]['branch_id'] == selected_branch]
        nodes.reverse()
        phases = [{**p, 'node_count': sum(locations[i['id']]['phase_id'] == p['id'] for i in visible)} for p in doc['phases']]
        return page(nodes, offset=offset, limit=limit, max_bytes=64000,
                    chat_id=chat_id, version=view['version'], head_version=view['head_version'],
                    selected_phase=selected, selected_branch=selected_branch,
                    order='newest_created_first', phases=phases, branches=doc['branches'],
                    focus=doc['overview']['focus'], node_count=len(visible), state=view['state'],
                    goals=[short_node(by_id[i], chat_id, view['version']) for i in doc['overview']['goal_item_ids'] if i in by_id],
                    next_steps=[short_node(by_id[i], chat_id, view['version']) for i in doc['overview']['next_step_ids'] if i in by_id])

    def versions(self, chat_id, offset=0, limit=20):
        self.store.require_human_chat(chat_id)
        integer(offset, 'offset', 0, 10**9)
        integer(limit, 'limit', 1, 50)
        rows = self.db.execute('SELECT version,origin,created_at FROM narrative_versions WHERE chat_id=? ORDER BY version DESC LIMIT ? OFFSET ?',
                               (chat_id, limit + 1, offset)).fetchall()
        result = page([dict(r) for r in rows], limit=limit, max_bytes=16000, chat_id=chat_id)
        result['retrieval']['offset'] = offset
        if result['retrieval']['next_offset'] is not None:
            result['retrieval']['next_offset'] += offset
        return result

    def changes(self, chat_id, version=None, offset=0, limit=20):
        view = Narrative(self.store).read(chat_id, version=version)
        selected = view['version']
        prior = self.db.execute('SELECT max(version) FROM narrative_versions WHERE chat_id=? AND version<?', (chat_id, selected)).fetchone()[0] or 0
        before = Narrative(self.store)._body(chat_id, prior) if prior else empty()
        after = Narrative(self.store)._body(chat_id, selected)
        diff = compare_documents(before, after, chat_id, prior, selected)
        rows = diff.pop('results')
        receipt = None
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='compact_batches'").fetchone():
            row = self.db.execute('SELECT base_version,published_version,turn_count,model_calls,input_tokens,output_tokens FROM compact_batches WHERE chat_id=? AND published_version=? AND state=?',
                                  (chat_id, selected, 'done')).fetchone()
            receipt = dict(row) if row else None
        return page(rows, offset=offset, limit=limit, max_bytes=64000, chat_id=chat_id,
                    before_version=prior, after_version=selected, receipt=receipt, **diff)

    def read(self, view, **kwargs):
        methods = {'mainline': self.mainline, 'chats': self.chats, 'narrative': self.narrative, 'versions': self.versions, 'changes': self.changes}
        if view not in methods:
            raise ValueError('unknown_panel_view')
        return methods[view](**kwargs)
