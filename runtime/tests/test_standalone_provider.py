import json
import os
from pathlib import Path
import pytest
from sessiongraph.provider_credentials import load_key, save_key, validate_endpoint
from sessiongraph.gateway_worker import gateway_credentials


def test_private_key_is_bound_to_exact_endpoint_and_safe_permissions(tmp_path):
    endpoint = "https://example.test/v1/responses"
    save_key(tmp_path, endpoint, "fixture-key")
    assert load_key(tmp_path, endpoint) == "fixture-key"
    assert load_key(tmp_path, "https://other.test/v1/responses") is None
    file = next((tmp_path / "credentials").iterdir())
    assert file.stat().st_mode & 0o777 == 0o600
    assert file.parent.stat().st_mode & 0o777 == 0o700
    file.unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    file.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        save_key(tmp_path, endpoint, "replacement")
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize("endpoint", ["http://example.test/v1/responses", "https://u:p@example.test/v1/responses", "https://example.test/v1/responses?key=x", "https://example.test/v1/chat/completions"])
def test_unsafe_endpoints_rejected(endpoint):
    with pytest.raises(ValueError):
        validate_endpoint(endpoint)


def test_direct_provider_never_reads_codex_tokens(tmp_path, monkeypatch):
    endpoint = "https://example.test/v1/responses"
    monkeypatch.setenv("SESSIONGRAPH_RUNTIME_HOME", str(tmp_path))
    monkeypatch.delenv("TEST_SG_KEY", raising=False)
    provider = {"provider": {"protocol": "responses", "endpoint": endpoint, "api_key_env": "TEST_SG_KEY"}}
    with pytest.raises(ValueError):
        with gateway_credentials(provider, tmp_path / "must-not-be-read.toml"):
            pytest.fail("No implicit remote credential")
    save_key(tmp_path, endpoint, "fixture-key")
    with gateway_credentials(provider, tmp_path / "must-not-be-read.toml"):
        assert os.environ["TEST_SG_KEY"] == "fixture-key"
    assert "TEST_SG_KEY" not in os.environ
