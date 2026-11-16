"""Real compact ingress: repeated stable paths and interrupted import retries."""

from seywo.archive import open_source

import json
from pathlib import Path
import stat

import pytest

from seywo import inbox
from seywo.config import initialize, load_config
from seywo.delta_pipeline import _import_claimed
from seywo.store import Store, digest


def transcript(path, text):
    records = [
        {"type": "session_meta", "payload": {"id": "chat-1"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": text}]}},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


@pytest.mark.parametrize("interrupt_after_import", [False, True])
def test_repeated_compact_copy_preserves_old_binding_and_resumes(
    tmp_path, monkeypatch, interrupt_after_import,
):
    config = load_config(Path(initialize(tmp_path / "project")["config"]))
    live = tmp_path / "session.jsonl"
    transcript(live, "first compact")
    first = inbox.record(config, "chat-1", "PreCompact", str(live))
    stable = Path(first["snapshot"])
    with Store(config) as store:
        assert _import_claimed(store, config, "chat-1", inbox.claim(config, first))
        # Reproduce an existing copy created by the old path-derived naming rule.
        old = stable.with_name(f"{stable.stem}-delta-{digest(str(stable))[:8]}{stable.suffix}")
        transcript(old, "older compact copy")
        old_bytes = old.read_bytes()
        store.import_source(old, "chat-1", 1)
        later = tmp_path / "later.jsonl"
        transcript(later, "intervening segment")
        store.import_source(later, "chat-1", 2)

        transcript(live, "new compact contents")
        from seywo.archive import capture
        # Simulate an old version/external writer replacing a bound locator.
        # The current archive API rejects such overwrites.
        staging = stable.with_name("external-replacement.jsonl")
        capture(live, staging)
        stable.write_bytes(staging.read_bytes())
        staging.unlink()
        monkeypatch.setattr(inbox, "snapshot", lambda *args, **kwargs: str(stable))
        item = inbox.record(config, "chat-1", "PreCompact", str(live))
        assert Path(item["snapshot"]) == stable
        claimed = inbox.claim(config, item)
        finish = inbox.finish
        if interrupt_after_import:
            def interrupted(*args, **kwargs):
                raise RuntimeError("process stopped after import")
            monkeypatch.setattr(inbox, "finish", interrupted)
            with pytest.raises(RuntimeError, match="process stopped"):
                _import_claimed(store, config, "chat-1", claimed)
            monkeypatch.setattr(inbox, "finish", finish)
            # The durable inbox binding must survive an import-before-ack crash.
            assert inbox.reclaim(config, "chat-1") == 1
            claimed = inbox.claim(config, inbox.pending(config, "chat-1")[0])
        assert _import_claimed(store, config, "chat-1", claimed)
        rows = store.db.execute(
            "SELECT ordinal,path FROM source_segments ORDER BY ordinal"
        ).fetchall()
        assert [r["ordinal"] for r in rows] == [0, 1, 2, 3]
        new_path = Path(rows[-1]["path"])
        assert new_path != old and new_path != stable
        assert open_source(new_path).read() == live.read_bytes()
        assert stat.S_IMODE(new_path.stat().st_mode) == 0o600
        assert old.read_bytes() == old_bytes
        saved = json.loads(Path(item["_path"]).read_text())
        assert saved["state"] == "done" and saved["snapshot"] == str(new_path)
