"""Clip retention: age expiry plus disk quota and free-space reserve (DESIGN §5.3).

Planning is a pure function so every rule is unit-testable:

1. Unpreserved clips past ``expires_at`` are deleted.
2. While total clip bytes exceed the quota, or the volume's free space is
   below the reserve, the **oldest unpreserved** clips are deleted.
3. Preserved clips are never deleted automatically. If they alone exceed the
   quota, or free space can't be restored, the plan carries warnings so the
   administrator sees it (heartbeat → dashboard).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vigil_edge.store import ClipRecord, LocalStore

log = logging.getLogger(__name__)

GB = 1024**3


@dataclass(frozen=True)
class RetentionPolicy:
    quota_bytes: int
    min_free_bytes: int


@dataclass
class RetentionPlan:
    delete: list[ClipRecord] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # clip id -> reason
    warnings: list[str] = field(default_factory=list)
    bytes_after: int = 0
    free_after: int = 0


def plan_retention(
    clips: Sequence[ClipRecord], now: datetime, policy: RetentionPolicy, free_bytes: int
) -> RetentionPlan:
    plan = RetentionPlan()
    remaining = sorted(clips, key=lambda c: c.created_at)
    total = sum(c.bytes for c in remaining)
    free = free_bytes

    def drop(c: ClipRecord, reason: str) -> None:
        nonlocal total, free
        plan.delete.append(c)
        plan.reasons[c.id] = reason
        total -= c.bytes
        free += c.bytes

    for c in list(remaining):
        if not c.preserved and c.expires_at <= now:
            drop(c, "expired")
            remaining.remove(c)

    for c in list(remaining):
        if total <= policy.quota_bytes and free >= policy.min_free_bytes:
            break
        if c.preserved:
            continue
        drop(c, "quota" if total > policy.quota_bytes else "low_free_space")
        remaining.remove(c)

    preserved = sum(c.bytes for c in remaining if c.preserved)
    if total > policy.quota_bytes:
        plan.warnings.append(
            f"preserved clips ({preserved / GB:.2f} GB) exceed the clip quota "
            f"({policy.quota_bytes / GB:.2f} GB); review preserved clips"
        )
    if free < policy.min_free_bytes:
        plan.warnings.append(
            f"free space {free / GB:.2f} GB is below the {policy.min_free_bytes / GB:.2f} GB "
            "reserve even after deleting unpreserved clips"
        )
    plan.bytes_after = total
    plan.free_after = free
    return plan


class RetentionManager:
    def __init__(self, store: LocalStore, clips_dir: Path, policy: RetentionPolicy) -> None:
        self.store = store
        self.clips_dir = clips_dir
        self.policy = policy
        self.last_warnings: list[str] = []

    def free_bytes(self) -> int:
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(self.clips_dir).free

    def run(self, now: datetime) -> RetentionPlan:
        plan = plan_retention(self.store.list_clips(), now, self.policy, self.free_bytes())
        for clip in plan.delete:
            path = Path(clip.path)
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:  # keep the row so we retry next time
                log.error("could not delete clip %s: %s", clip.id, exc)
                continue
            self.store.delete_clip(clip.id)
            log.info("deleted clip %s (%s, %d bytes)", clip.id, plan.reasons[clip.id], clip.bytes)
        for w in plan.warnings:
            log.warning("storage: %s", w)
        self.last_warnings = plan.warnings
        return plan
