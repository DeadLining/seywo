"""Acceptance metrics must count native Codex calls, including unknown usage."""
import importlib.util
from pathlib import Path

import pytest


def benchmark():
    path = Path(__file__).resolve().parents[1] / 'evals/compact_benchmark.py'
    spec = importlib.util.spec_from_file_location('compact_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('usage', [{'input_tokens': 123, 'output_tokens': 45}, None])
def test_native_calls_and_usage_are_measured_without_real_provider(monkeypatch, usage):
    module = benchmark()
    transport_before = module.maintain.CodexTransport

    class NativeFixture(module.CodexTransport):
        def complete_compact(self, *, instructions, payload, model, **kwargs):
            return self._execute(instructions, payload, {}, model=model)

        def _execute(self, instructions, payload, schema, *, model=None):
            indexes = [turn['index'] for turn in payload['turns']]
            return {'finish': 'stop', 'error': None, 'usage': usage, 'output': {
                'segments': [{'title': '账单审阅', 'turn_indexes': indexes,
                              'action': 'continue', 'anchor_index': 0}],
                'changes': [{'operation': 'new', 'title': '账单审阅', 'type': 'goal',
                             'attribution': 'user', 'turn_indexes': indexes, 'target_index': None,
                             'open': True, 'merge_indexes': []}],
                'current': {'goal_indexes': [0], 'question_indexes': [], 'next_step_indexes': []}}}

    monkeypatch.setattr(module, 'CodexTransport', NativeFixture)
    case = {'id': 'meter-native', 'checks': {'max_items': 1, 'phases': 1},
            'tools': True, 'windows': [[('审阅账单。', '开始账单审阅。')]]}
    provider = {'backend': 'codex', 'model': 'codex-default', 'timeout': 420, 'max_output_tokens': 768}
    row = module.run_case(case, 'seywo-compact-v1', provider, 0)
    assert row['state'] == 'done', row['error_code']
    assert row['calls'] == 1
    assert row['checks']['call_accounting']
    assert row['input_tokens'] == (123 if usage else None)
    assert row['output_tokens'] == (45 if usage else None)
    assert row['checks']['usage_measured'] is bool(usage)
    assert row['stored_tool_records'] == 2  # source metadata remains retrievable
    assert row['tool_body_excluded']
    assert module.maintain.CodexTransport is transport_before
