"""Replay captured native model results only when request and schema match exactly.

No provider calls. Useful for runtime fixes: changed downstream inputs fail closed
instead of pretending an old response was generated for a new request.
"""
import argparse
from copy import deepcopy
from datetime import date
import json
from pathlib import Path

from compact_benchmark import CodexTransport, encode, run_case
import compact_benchmark as benchmark


def replay(directory, output):
    captured = json.loads((directory / 'report.json').read_text())
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for repetition in range(captured['repetitions']):
        for case in captured['fixtures']:
            name = f"{case['id']}-seywo-compact-v1-{repetition}.json"
            original = json.loads((directory / name).read_text())
            traces = original['traces']
            position = 0

            class Captured(CodexTransport):
                def _execute(self, instructions, payload, schema, *, model=None):
                    nonlocal position
                    if position >= len(traces):
                        raise ValueError('replay_capture_exhausted')
                    trace = traces[position]
                    expected = {'model': model, 'instructions': instructions, 'payload': payload, 'schema': schema}
                    if encode(expected) != encode(trace['request']):
                        raise ValueError('replay_request_drift')
                    position += 1
                    return deepcopy(trace['result'])

            previous = benchmark.CodexTransport
            benchmark.CodexTransport = Captured
            try:
                row = run_case(case, 'seywo-compact-v1', {
                    'backend': 'codex', 'model': captured['model'], 'timeout': 420, 'max_output_tokens': 768}, repetition)
            finally:
                benchmark.CodexTransport = previous
            row['checks']['all_captured_calls_used'] = position == len(traces)
            row['evaluation_mode'] = 'exact_captured_replay'
            row['new_provider_calls'] = 0
            row['captured_result_file'] = str(directory / name)
            (output / name).write_text(json.dumps(row, ensure_ascii=False, indent=2))
            compact = {k: row[k] for k in ('case', 'state', 'checks', 'calls', 'input_tokens', 'output_tokens', 'items', 'error_code')}
            rows.append(compact)
            print(encode(compact), flush=True)
    report = {'date': date.today().isoformat(), 'evaluation_mode': 'exact_captured_replay',
              'new_provider_calls': 0, 'capture_directory': str(directory), 'runs': rows,
              'passed': sum(all(row['checks'].values()) for row in rows), 'count': len(rows),
              'limits': ['Token counts belong to captured original calls, not this replay.',
                         'Every replayed request, including downstream state and schema, must match exactly.']}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = replay(args.capture, args.output)
    print(encode({k: v for k, v in result.items() if k != 'runs'}))
    raise SystemExit(result['passed'] != result['count'])
