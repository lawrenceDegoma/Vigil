"""Incident clip upload: only alerted-incident clips, after their events, in parts."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from helpers import T0
from vigil_edge.clip_upload import MAX_ATTEMPTS, ClipUploader, ffmpeg_split
from vigil_edge.clock import FakeClock
from vigil_edge.config import CloudConfig
from vigil_edge.recording import sha256_file
from vigil_edge.store import ClipRecord, LocalStore
from vigil_edge.sync import SyncError

TOKEN = "vgl_" + "b" * 43
CFG = CloudConfig(enabled=True, endpoint="https://abc.supabase.co/functions/v1")


class Server:
    """Fake clip-upload function plus storage PUT endpoint."""

    def __init__(self, register: dict[str, Any] | None = None, complete: str = "ok") -> None:
        self.register_reply = register
        self.complete_reply = complete
        self.posts: list[dict[str, Any]] = []
        self.puts: list[tuple[str, int, dict[str, str]]] = []
        self.fail: Exception | None = None

    def transport(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        if self.fail:
            raise self.fail
        assert url == "https://abc.supabase.co/functions/v1/clip-upload"
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        req = json.loads(body)
        self.posts.append(req)
        if req["action"] == "register":
            reply = self.register_reply or {
                "status": "ok",
                "parts": [
                    {
                        "index": p["index"],
                        "upload_url": f"/storage/v1/object/upload/sign/x/{p['index']}?t=1",
                    }
                    for p in req["clip"]["parts"]
                ],
            }
            return 200, json.dumps(reply).encode()
        return 200, json.dumps({"status": self.complete_reply, "complete": True}).encode()

    def put(self, url: str, path: Path, size: int, headers: dict[str, str], timeout: float) -> int:
        assert path.stat().st_size == size
        self.puts.append((url, size, headers))
        return 200


def clip(
    tmp_path: Path, store: LocalStore, cid: str, kind: str = "incident", size: int = 1000
) -> ClipRecord:
    f = tmp_path / f"{cid}.mp4"
    f.write_bytes(b"\x00" * size)
    rec = ClipRecord(
        id=cid, path=str(f), incident_ids=("b0000000-0000-0000-0000-000000000001",), kind=kind,
        started_at=T0, ended_at=T0 + timedelta(seconds=60), bytes=size, sha256=sha256_file(f),
        created_at=T0, expires_at=T0 + timedelta(days=30),
    )  # fmt: skip
    store.add_clip(rec)
    return rec


def uploader(store: LocalStore, server: Server, **cfg: Any) -> ClipUploader:
    return ClipUploader(
        store, replace(CFG, **cfg), TOKEN, FakeClock(T0), server.transport, server.put
    )


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path / "v.db")


def test_small_clip_uploads_in_one_part(tmp_path: Path, store: LocalStore) -> None:
    rec = clip(tmp_path, store, "c1")
    server = Server()
    assert uploader(store, server).upload_next() == "uploaded"
    assert store.cloud_state("c1") == "uploaded"
    [reg, done] = server.posts
    assert reg["clip"]["parts"] == [{"index": 0, "bytes": 1000, "sha256": rec.sha256}]
    assert done == {"action": "complete", "clip_id": "c1", "part_index": 0}
    [(url, size, headers)] = server.puts
    assert url == "https://abc.supabase.co/storage/v1/object/upload/sign/x/0?t=1"
    assert headers["Content-Type"] == "video/mp4" and size == 1000


def test_waits_for_incident_events_and_ignores_shadow_clips(
    tmp_path: Path, store: LocalStore
) -> None:
    clip(tmp_path, store, "shadow", kind="shadow")
    up = uploader(store, Server())
    assert up.upload_next() == "idle"  # shadow clips never upload
    clip(tmp_path, store, "c1")
    store.enqueue("incident_event", {"event_id": "e"}, T0, "e")
    assert up.upload_next() == "waiting"
    store.mark_sent([m.seq for m in store.pending()], T0)
    assert up.upload_next() == "uploaded"
    assert store.cloud_state("shadow") is None


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        ({"status": "not_eligible"}, "skipped:not_alerted"),
        ({"status": "disabled"}, "skipped:disabled"),
    ],
)
def test_ineligible_clips_stay_local(
    tmp_path: Path, store: LocalStore, reply: dict[str, Any], state: str
) -> None:
    clip(tmp_path, store, "c1")
    server = Server(register=reply)
    assert uploader(store, server).upload_next() == "skipped"
    assert store.cloud_state("c1") == state
    assert server.puts == []


def test_transient_problems_retry_then_give_up(tmp_path: Path, store: LocalStore) -> None:
    clip(tmp_path, store, "c1")
    server = Server(register={"status": "unknown_incident"})
    up = uploader(store, server)
    results = [up.upload_next() for _ in range(MAX_ATTEMPTS)]
    assert results[:-1] == ["retry"] * (MAX_ATTEMPTS - 1) and results[-1] == "skipped"
    assert store.cloud_state("c1") == "skipped:unknown_incident"


def test_network_failure_keeps_the_clip_pending(tmp_path: Path, store: LocalStore) -> None:
    clip(tmp_path, store, "c1")
    server = Server()
    server.fail = SyncError("refused")
    up = uploader(store, server)
    assert up.upload_next() == "retry"
    assert store.cloud_state("c1") is None and up.next_attempt > 0
    server.fail = None
    assert up.upload_next() == "uploaded"


def test_size_mismatch_is_retried(tmp_path: Path, store: LocalStore) -> None:
    clip(tmp_path, store, "c1")
    server = Server(complete="size_mismatch")
    assert uploader(store, server).upload_next() == "retry"
    assert store.cloud_state("c1") is None


def test_already_uploaded_parts_are_not_sent_again(tmp_path: Path, store: LocalStore) -> None:
    clip(tmp_path, store, "c1")
    server = Server(register={"status": "ok", "parts": [{"index": 0, "upload_url": None}]})
    assert uploader(store, server).upload_next() == "uploaded"
    assert server.puts == []
    assert server.posts[-1]["action"] == "complete"


def test_missing_file_is_skipped(tmp_path: Path, store: LocalStore) -> None:
    rec = clip(tmp_path, store, "c1")
    Path(rec.path).unlink()  # e.g. local retention removed it
    assert uploader(store, Server()).upload_next() == "skipped"
    assert store.cloud_state("c1") == "skipped:missing"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg"
)
def test_large_clip_is_split_into_playable_parts_under_the_limit(
    tmp_path: Path, store: LocalStore
) -> None:
    src = tmp_path / "big.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=640x360:rate=15,noise=alls=60:allf=t", "-t", "20",
         # constant bitrate, 1 s keyframe groups, no B-frames: like the Pi's hardware encoder
         "-c:v", "libx264", "-b:v", "1500k", "-minrate", "1500k", "-maxrate", "1500k",
         "-bufsize", "750k", "-bf", "0", "-x264-params", "keyint=15:min-keyint=15:scenecut=0",
         "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )  # fmt: skip
    size = src.stat().st_size
    assert size > 2 * 1024 * 1024, size
    rec = ClipRecord(
        id="big", path=str(src), incident_ids=("b0000000-0000-0000-0000-000000000001",),
        kind="incident", started_at=T0, ended_at=T0 + timedelta(seconds=20), bytes=size,
        sha256=sha256_file(src), created_at=T0, expires_at=T0 + timedelta(days=30),
    )  # fmt: skip
    store.add_clip(rec)
    server = Server()
    assert uploader(store, server, max_upload_mb=1.0).upload_next() == "uploaded"
    parts = server.posts[0]["clip"]["parts"]
    assert len(parts) >= 3
    assert all(p["bytes"] <= 1024 * 1024 for p in parts)
    assert [p["index"] for p in parts] == list(range(len(parts)))
    assert len(server.puts) == len(parts)
    assert not list(tmp_path.glob("vigil-upload-*")), "temporary parts are cleaned up"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg"
)
def test_split_parts_play_and_cover_the_clip(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=15",
         "-t", "12", "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "parts"
    out.mkdir()
    files = ffmpeg_split(src, 4.0, out)
    durations = [
        float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(f)],
            capture_output=True, text=True, check=True,
        ).stdout)
        for f in files
    ]  # fmt: skip
    assert len(files) == 3
    assert abs(sum(durations) - 12.0) < 0.5
