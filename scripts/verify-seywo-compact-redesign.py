#!/usr/bin/env python3
# ruff: noqa: E402
"""Real native model comparison on identical dialogue; never changes project data."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'seywo/src'))
from seywo.compact_model import instructions, request_payload, validate, SCHEMA
from seywo.compact_pipeline import apply_compact
from seywo.codex_transport import CodexTransport
from seywo.narrative import empty


def seed():
    doc = empty()
    doc['phases'] = [{'id': 'phase', 'title': '插件交付验收', 'goal_item_ids': ['goal']}]
    doc['overview'].update(focus={'phase_id': 'phase', 'branch_id': None},
        goal_item_ids=['goal'], next_step_ids=['hook', 'compact'],
        unresolved=['旧服务是否停用', 'Hook与依赖安装待验收'])
    for identity, title, kind in [('goal', '插件交付验收', 'goal'),
            ('hook', 'Hook与依赖安装待验收', 'action'),
            ('compact', '验证真实Compact', 'next_step'), ('old', '旧服务是否停用', 'question')]:
        doc['items'].append({'id': identity, 'title': title, 'content': title, 'type': kind,
            'status': 'open', 'hidden': False, 'attribution': 'user_statement', 'evidence': [],
            'dependencies': [], 'scope': 'chat'})
        doc['placements'].append({'item_id': identity, 'phase_id': 'phase', 'branch_id': None})
    return doc


def dialogue(user, assistant):
    sources = [{'event_id': 'u', 'snapshot_id': 'snapshot', 'start': 0, 'end': len(user), 'role': 'user'},
               {'event_id': 'a', 'snapshot_id': 'snapshot', 'start': 0, 'end': len(assistant), 'role': 'assistant'}]
    return [{'index': 0, 'turn_id': 'turn', 'user': user, 'assistant': assistant, 'sources': sources}]


def main():
    dest = Path(sys.argv[1])
    baseline = Path(os.environ.get('SEYWO_BASELINE_ROOT', str(REPO / 'dist/seywo-plugin-0.3.0/seywo')))
    spec = importlib.util.spec_from_file_location('seywo._baseline_compact_model', baseline / 'src/seywo/compact_model.py')
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
    transport_spec = importlib.util.spec_from_file_location('seywo._baseline_codex_transport', baseline / 'src/seywo/codex_transport.py')
    old_transport_module = importlib.util.module_from_spec(transport_spec)
    transport_spec.loader.exec_module(old_transport_module)
    old_transport = old_transport_module.CodexTransport()
    doc = seed()
    catalog = [{'index': n, **i} for n, i in enumerate(doc['items'])]
    turns = dialogue('Hook和依赖安装都已验收通过。现在只需要验证真实Compact，旧服务问题本次不再关注。',
                     'Hook与依赖安装已完成验收；下一步验证真实Compact。')
    payload = request_payload(turns, {'focus': '插件交付验收', 'goals': ['插件交付验收'], 'open': []}, catalog)
    transport = CodexTransport()
    runs = []
    for label, rules, schema in [('before', old.instructions(), old.SCHEMA), ('after', instructions(), SCHEMA)]:
        result = (old_transport if label == 'before' else transport)._execute(rules, payload, schema)
        assert result.get('output') is not None and not result.get('error'), result
        if label == 'after':
            output = validate(result['output'], [0], set(range(len(catalog))))
            revised, added, touched = apply_compact(doc, output, turns, catalog, 'real-comparison')
            by_id = {i['id']: i for i in revised['items']}
            assert by_id['hook']['status'] == 'resolved', result['output']
            assert 'old' not in revised['overview']['open_question_ids'], result['output']
            assert 'compact' in revised['overview']['next_step_ids'], result['output']
            assert len(revised['items']) <= len(doc['items']) + 1
        runs.append({'case': label, 'instructions_chars': len(rules), 'schema_chars': len(json.dumps(schema)),
                     'usage': result['usage'], 'model_info': result.get('model_info'),
                     'cached_input_tokens': result.get('cached_input_tokens'), 'seconds': result['seconds'],
                     'output': result['output']})
        dest.write_text(json.dumps({'runs': runs, 'complete': False}, ensure_ascii=False, indent=2))
    # A partial result must not silently close a composite historical task.
    compound = dialogue('Hook已启用且可信。依赖自动安装还没验证，不能认为整项验收完成。',
                        '已确认Hook启用；依赖自动安装仍需验证。下一步继续验证真实Compact。')
    result = transport.complete_compact(model=transport.model, instructions=instructions(),
        payload=request_payload(compound, {'focus': '插件交付验收'}, catalog))
    assert not result.get('error'), result
    output = validate(result['output'], [0], set(range(len(catalog))))
    revised, _, _ = apply_compact(doc, output, compound, catalog, 'real-compound')
    assert next(i for i in revised['items'] if i['id'] == 'hook')['status'] == 'open', output
    runs.append({'case': 'compound_goal_partial_completion', 'usage': result['usage'],
                 'model_info': result.get('model_info'), 'output': output, 'seconds': result['seconds']})
    before, after = runs[0]['usage']['input_tokens'], runs[1]['usage']['input_tokens']
    dest.write_text(json.dumps({'complete': True, 'verified_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'calls': len(runs), 'same_dialogue_comparison': True, 'input_token_reduction': round(1-after/before, 4),
        'project_data_modified': False, 'runs': runs}, ensure_ascii=False, indent=2))
    print(dest.read_text())


if __name__ == '__main__':
    main()
