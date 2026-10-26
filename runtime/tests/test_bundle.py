"""Bundle only executable source; no data, tests, cache or virtual environments."""
import json
from pathlib import Path


def test_gateway_bundle_manifest_has_exact_source_allowlist():
    vendor=Path(__file__).resolve().parents[1]
    gateway=vendor.parent
    resources=json.loads((gateway/'src-tauri/tauri.conf.json').read_text())['bundle']['resources']
    actual={key.removeprefix('../runtime/') for key in resources if key.startswith('../runtime/')}
    expected={'entrypoint.py'}
    for folder in ['sessiongraph','hooks','scripts']:
        expected.update(p.relative_to(vendor).as_posix() for p in (vendor/folder).iterdir() if p.is_file() and (p.suffix in {'.py','.sh'} or p.name=='policy.json'))
    assert actual==expected
    assert all(resources['../runtime/'+rel]=='sessiongraph/'+rel for rel in expected)
    assert all('*' not in key for key in actual)
