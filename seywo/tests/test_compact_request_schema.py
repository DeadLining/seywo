"""Offer only request-local identities to both generation backends."""
from copy import deepcopy

from seywo import compact_model
from seywo.codex_transport import CodexTransport
from seywo.transport import Transport


def payload():
    return {'turns': [{'index': 0}, {'index': 1}, {'index': 2}],
            'active_items': [{'index': 4}, {'index': 9}]}


def test_generation_schema_bounds_turn_references_without_mutating_static_schema():
    original = deepcopy(compact_model.SCHEMA)
    schema = compact_model.schema_for(payload())
    for field in ['segments', 'changes']:
        assert schema['properties'][field]['items']['properties']['turn_indexes']['items']['enum'] == [0, 1, 2]
    assert compact_model.SCHEMA == original


def test_shared_identity_domains_include_history_and_possible_new_items():
    schema = compact_model.schema_for(payload())
    identities = [4, 9] + list(range(10, 10 + compact_model.MAX_CHANGES))
    assert schema['properties']['segments']['items']['properties']['anchor_index']['enum'] == identities + [None]
    assert schema['properties']['changes']['items']['properties']['target_index']['enum'] == identities + [None]
    assert schema['properties']['changes']['items']['properties']['merge_indexes']['items']['enum'] == [4, 9]
    current = schema['properties']['current']['properties']
    assert current['goal_index']['enum'] == identities + [None]
    assert current['priority_indexes']['items']['enum'] == identities


def test_both_model_backends_send_the_same_request_bound_schema(monkeypatch):
    offered = []
    monkeypatch.setattr(CodexTransport, '_execute', lambda self, instructions, payload, schema, **kw: offered.append(schema))
    monkeypatch.setattr(Transport, '_complete_json', lambda self, **kw: offered.append(kw['schema']))
    CodexTransport().complete_compact(model='fixture', instructions='fixture', payload=payload())
    Transport('http://127.0.0.1:1', 'fixture', 'fixture').complete_compact(model='fixture', instructions='fixture', payload=payload())
    assert offered == [compact_model.schema_for(payload())] * 2
