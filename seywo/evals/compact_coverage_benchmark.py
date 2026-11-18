"""Real-model probes for lossless bounded input, provenance and condensation.

Imports only synthetic dialogue into temporary projects, using explicit Seywo
configuration. This script calls the configured model; it is not an offline test.
"""
import argparse
from datetime import date
import json
from pathlib import Path

from compact_benchmark import run_case
from seywo.config import load_config
from seywo.settings import read_provider


CASES = [
    {'id': 'user-middle-requirement', 'checks': {'max_items': 3, 'phases': 1, 'mainline': True},
     'windows': [[(
         '目标是交付月度对账功能，功能尚未完成。以下参考编号不形成工作事项：\n'
         + 'entry. ' * 180 + '\n月结口径改为自然月。\n' + 'entry. ' * 180
         + '\n只记录工作目标与结算口径。', '已记录，月度对账功能仍待实现。')]]},
    {'id': 'assistant-middle-pending', 'checks': {'max_items': 3, 'phases': 1, 'mainline': True},
     'windows': [[('完成数据导出验收，关注尚未通过的检查。',
         '流式写出已实现。以下参考编号不形成额外事项。\n' + 'record. ' * 160
         + '\n中文兼容性尚未验证，整体验收仍未完成。\n' + 'record. ' * 160
         + '\n继续核验。')]]},
    {'id': 'native-turn-crosses-requests', 'checks': {'max_items': 3, 'phases': 1, 'mainline': True},
     'windows': [[(
         '目标是交付月度对账功能，功能尚未完成。以下参考编号不形成工作事项：\n'
         + 'entry. ' * 1400 + '\n月结口径改为自然月。\n' + 'entry. ' * 1400
         + '\n只记录工作目标与结算口径，继续完成尚未实现的对账功能。',
         '收到，月度对账仍待实现，结算口径按自然月。')]]},
    {'id': 'short-completion-update', 'checks': {'max_items': 1, 'phases': 1, 'mainline': True},
     'windows': [[('修复缓存异常。', '缓存异常尚未修复。')], [('已完成', '收到')]]},
]


def inspect(row, case):
    offered = [turn for trace in row['traces'] for turn in trace['request']['payload']['turns']]
    originals = [pair for window in case['windows'] for pair in window]
    checks = row['checks']
    for role, position in [('user', 0), ('assistant', 1)]:
        checks[f'exact_{role}_coverage'] = ''.join(t[role] for t in offered) == ''.join(p[position] for p in originals)
    checks['bounded_role_slices'] = all(len(t['user']) <= 1200 and len(t['assistant']) <= 2200 for t in offered)
    visible = [item for item in row['document']['items'] if not item.get('hidden')]
    titles = ' '.join(item['title'] for item in visible)
    if case['id'] in {'user-middle-requirement', 'native-turn-crosses-requests'}:
        checks['middle_month_requirement_retained'] = '自然月' in titles
        checks['unfinished_goal_remains_open'] = any(i['status'] == 'open' and '对账' in i['title'] for i in visible)
    if case['id'] == 'assistant-middle-pending':
        checks['middle_pending_check_retained'] = any(i['status'] == 'open' and '兼容' in i['title'] for i in visible)
    if case['id'] == 'short-completion-update':
        checks['short_completion_closes_work'] = bool(visible) and all(i['status'] == 'resolved' for i in visible)
        checks['one_stable_identity'] = len(visible) == 1
    checks['one_call_for_small_native_turn'] = row['calls'] == (2 if case['id'] in {'native-turn-crosses-requests', 'short-completion-update'} else 1)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--case', action='append', dest='cases')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    provider = read_provider(load_config(args.config))
    cases = [case for case in CASES if not args.cases or case['id'] in args.cases]
    rows = []
    for case in cases:
        row = inspect(run_case(case, 'seywo-compact-v1', provider, 0), case)
        (args.output / (case['id'] + '.json')).write_text(json.dumps(row, ensure_ascii=False, indent=2))
        compact = {key: row[key] for key in ('case', 'state', 'checks', 'calls', 'input_tokens', 'output_tokens', 'titles', 'items', 'error_code')}
        rows.append(compact)
        print(json.dumps(compact, ensure_ascii=False), flush=True)
    report = {'date': date.today().isoformat(), 'cases': cases, 'runs': rows,
              'passed': sum(all(row['checks'].values()) for row in rows), 'count': len(rows),
              'limits': ['Synthetic model probes do not establish general semantic accuracy.',
                         'Token usage measures provider reporting, not hardware energy.']}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(report['passed'] != report['count'])


if __name__ == '__main__':
    main()
