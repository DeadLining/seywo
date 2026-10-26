"""Provider-free contract tests, not evidence of real model extraction quality."""
import json

import pytest

from sessiongraph.auto_update import run_update
from sessiongraph.continuity import refresh_chat
from sessiongraph.core import Store, init_project, load_config
from sessiongraph.memories import run_chat
from sessiongraph.work_map import work_map
from test_memories import FakeExtractor
from test_mvp import write_session


def test_import_goal_then_artifacts_and_produces_across_batches(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    logs = tmp_path / 'logs'
    logs.mkdir()
    config_path = init_project(root)
    cfg = json.loads(config_path.read_text())
    cfg['hook']['allowed_transcript_roots'] = [str(logs)]
    config_path.write_text(json.dumps(cfg))
    store = Store(load_config(config_path))
    write_session(logs / 'first-chat-A.jsonl', root, 'chat-A', [('user', '目标：训练复盘')])
    write_session(logs / 'foreign-chat-A.jsonl', tmp_path / 'other', 'chat-A', [('user', '不导入')])
    extractors = []
    def execute(argv, timeout):
        if 'refresh-chat' in argv:
            return refresh_chat(store, 'chat-A')
        return run_chat(store, extractors[-1], 'chat-A')
    def item(kind, title, text, eid, status='completed'):
        return dict(kind=kind, title=title, content=text, status=status, aliases=[],
                    entry_id=eid, quote=text)
    # Read exact source IDs in the fake provider just as a provider receives them.
    class GoalExtractor(FakeExtractor):
        def __call__(self, batch, policy):
            self.calls.append((batch, policy))
            row = next(r for r in batch if r['text'] == '目标：训练复盘')
            return {'memories': [item('goal', '训练复盘', row['text'], row['id'], 'active')]}
    extractors.append(GoalExtractor([]))
    prepared = run_update(config_path, 'chat-A', 'PreCompact', execute=execute)
    assert prepared['state'] == 'prepared' and prepared['reported_model_calls'] == 0
    first = run_update(config_path, 'chat-A', 'PostCompact', execute=execute)
    assert first['state'] == 'succeeded' and first['saved_count'] == 1
    goal = store.db.execute('select id from work_memory').fetchone()[0]
    text = '训练复盘已产出操作手册 /project/handbook.md 和实验记录模板 /project/template.md。'
    write_session(logs / 'second-chat-A.jsonl', root, 'chat-A', [('assistant', text)])
    class ArtifactExtractor(FakeExtractor):
        def __call__(self, batch, policy):
            self.calls.append((batch, policy))
            assert goal in {m['id'] for m in policy['existing_memories']}
            row = next(r for r in batch if r['text'] == text)
            titles = ['操作手册', '实验记录模板']
            return {'memories': [item('artifact', title, text, row['id']) for title in titles],
                    'relations': [dict(source=goal, target=f'new:{i}', kind='PRODUCES',
                                       entry_id=row['id'], quote=text,
                                       source_mention='训练复盘', target_mention=title)
                                  for i, title in enumerate(titles)]}
    ext = ArtifactExtractor([])
    extractors.append(ext)
    result = run_update(config_path, 'chat-A', 'PostCompact', execute=execute)
    assert result['state'] == 'succeeded'
    assert result['saved_count'] == 2 and result['saved_relation_count'] == 2
    assert result['rejected_relation_count'] == 0
    before = store.db.total_changes
    graph = work_map(store, chat='chat-A')
    assert graph['stats']['semantic_relations'] == 2 and graph['model_calls'] == 0
    assert len([n for n in graph['nodes'] if n['kind'] == 'artifact']) == 2
    assert store.recall('操作手册')['relations']
    assert store.db.total_changes == before
    rerun = run_update(config_path, 'chat-A', 'PostCompact', execute=execute)
    assert rerun['saved_count'] == 0 and rerun['saved_relation_count'] == 0
    assert len(ext.calls) == 1  # committed cursor means no provider re-call on replay
    assert store.db.execute('select count(*) from work_memory_relations').fetchone()[0] == 2
    store.close()


@pytest.mark.parametrize('invalid', [False, True])
def test_artifacts_survive_without_relation_or_when_relation_is_rejected(tmp_path, invalid):
    from test_memories import make_store
    text = '已整理好操作手册，另有实验记录模板。'
    _, store, _ = make_store(tmp_path, [('assistant', text)])
    eid = store.db.execute('select id from entries').fetchone()[0]
    memories = [dict(kind='artifact', title=t, content=text, status='completed', aliases=[],
                     entry_id=eid, quote=text) for t in ['操作手册', '实验记录模板']]
    relations = ([dict(source='new:0', target='new:1', kind='PRODUCES', entry_id=eid,
                       quote=text, source_mention='操作手册', target_mention='实验记录模板')]
                 if invalid else [])
    result = run_chat(store, FakeExtractor([dict(memories=memories, relations=relations)]), 'chat-1')
    assert not result['failed'] and len(result['saved']) == 2
    assert not result['saved_relations']
    assert len(result['rejected_relations']) == int(invalid)
    assert work_map(store)['stats']['semantic_relations'] == 0
    store.close()
