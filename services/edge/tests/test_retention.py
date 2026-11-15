from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from helpers import T0, clip
from vigil_edge.retention import RetentionManager, RetentionPolicy, plan_retention
from vigil_edge.store import LocalStore

BIG = 10**12


def ids(plan) -> list[str]:  # type: ignore[no-untyped-def]
    return [c.id for c in plan.delete]


def test_expired_unpreserved_clips_are_deleted() -> None:
    clips = [clip(1, expires_at=T0 - timedelta(days=1)), clip(2)]
    plan = plan_retention(clips, T0, RetentionPolicy(BIG, 0), free_bytes=BIG)
    assert ids(plan) == ["c1"]
    assert plan.reasons["c1"] == "expired"


def test_preserved_clips_never_expire() -> None:
    clips = [clip(1, expires_at=T0 - timedelta(days=1), preserved_at=T0)]
    assert ids(plan_retention(clips, T0, RetentionPolicy(BIG, 0), BIG)) == []


def test_quota_deletes_oldest_unpreserved_first() -> None:
    clips = [clip(1, preserved_at=T0), clip(2), clip(3), clip(4)]  # 1k,2k,3k,4k bytes
    plan = plan_retention(clips, T0, RetentionPolicy(quota_bytes=6000, min_free_bytes=0), BIG)
    assert ids(plan) == ["c2", "c3"]  # c1 preserved; 10k -> 5k
    assert plan.bytes_after == 5000
    assert not plan.warnings


def test_low_free_space_deletes_until_reserve() -> None:
    clips = [clip(1), clip(2), clip(3)]
    plan = plan_retention(clips, T0, RetentionPolicy(BIG, min_free_bytes=2500), free_bytes=0)
    assert ids(plan) == ["c1", "c2"]
    assert plan.reasons["c1"] == "low_free_space"


def test_warns_when_preserved_clips_exceed_quota() -> None:
    clips = [clip(5, preserved_at=T0), clip(1)]
    plan = plan_retention(clips, T0, RetentionPolicy(quota_bytes=3000, min_free_bytes=0), BIG)
    assert ids(plan) == ["c1"]
    assert any("preserved clips" in w for w in plan.warnings)


def test_warns_when_free_space_cannot_be_restored() -> None:
    plan = plan_retention([clip(1, preserved_at=T0)], T0, RetentionPolicy(BIG, 10**9), 0)
    assert any("free space" in w for w in plan.warnings)


def test_manager_deletes_files_and_rows(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "v.db")
    f = tmp_path / "old.mp4"
    f.write_bytes(b"x" * 10)
    store.add_clip(clip(1, path=str(f), expires_at=T0 - timedelta(seconds=1)))
    store.add_clip(clip(2, path=str(tmp_path / "missing.mp4"), expires_at=T0 - timedelta(1)))
    mgr = RetentionManager(store, tmp_path / "clips", RetentionPolicy(BIG, 0))
    plan = mgr.run(T0)
    assert sorted(ids(plan)) == ["c1", "c2"]
    assert not f.exists()
    assert store.list_clips() == []
