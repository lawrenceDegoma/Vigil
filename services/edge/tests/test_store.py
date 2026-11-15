from __future__ import annotations

from pathlib import Path

from helpers import T0, clip
from vigil_edge.store import LocalStore


def test_clip_roundtrip_and_preserve(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "v.db")
    s.add_clip(clip(1))
    s.add_clip(clip(2))
    assert [c.id for c in s.list_clips()] == ["c1", "c2"]
    assert s.get_clip("c1") == clip(1)
    assert s.clips_bytes() == 3000
    assert s.set_preserved("c2", True, T0)
    got = s.get_clip("c2")
    assert got is not None and got.preserved
    assert not s.set_preserved("missing", True, T0)
    s.delete_clip("c1")
    assert [c.id for c in s.list_clips()] == ["c2"]


def test_outbox_is_idempotent_and_ordered(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "v.db")
    assert s.enqueue("incident_event", {"n": 1}, T0, dedup_key="e1")
    assert not s.enqueue("incident_event", {"n": 1}, T0, dedup_key="e1")  # replay
    s.enqueue("heartbeat", {"n": 2}, T0)
    pending = s.pending()
    assert [m.payload["n"] for m in pending] == [1, 2]
    s.record_attempt([pending[0].seq])
    s.mark_sent([pending[0].seq], T0)
    assert s.queue_depth() == 1
    assert s.pending()[0].payload == {"n": 2}


def test_outbox_trim_never_drops_incident_events(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "v.db")
    for i in range(5):
        s.enqueue("incident_event", {"i": i}, T0, dedup_key=f"e{i}")
    for i in range(10):
        s.enqueue("heartbeat", {"h": i}, T0)
    removed = s.trim_outbox(max_rows=8)
    assert removed == 7
    kinds = [m.kind for m in s.pending(100)]
    assert kinds.count("incident_event") == 5
    assert [m.payload["h"] for m in s.pending(100) if m.kind == "heartbeat"] == [7, 8, 9]


def test_persists_across_reopen(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "v.db")
    s.enqueue("incident_event", {"x": 1}, T0, dedup_key="e")
    s.close()
    assert LocalStore(tmp_path / "v.db").queue_depth() == 1
