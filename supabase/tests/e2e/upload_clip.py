"""E2E helper: upload one clip with the real edge ClipUploader (run by clips_e2e_test.ts).

usage: upload_clip.py ENDPOINT TOKEN CLIP_ID INCIDENT_ID FILE WORKDIR [MAX_UPLOAD_MB]
Prints the uploader's result and the clip's resulting cloud state.
"""

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vigil_edge.clip_upload import ClipUploader
from vigil_edge.clock import SystemClock
from vigil_edge.config import CloudConfig
from vigil_edge.recording import sha256_file
from vigil_edge.store import ClipRecord, LocalStore

endpoint, token, clip_id, incident_id, file, workdir = sys.argv[1:7]
max_mb = float(sys.argv[7]) if len(sys.argv) > 7 else 45.0
path = Path(file)
store = LocalStore(Path(workdir) / "vigil.db")
now = datetime.now(UTC)
store.add_clip(
    ClipRecord(
        id=clip_id,
        path=str(path),
        incident_ids=(incident_id,),
        kind="incident",
        started_at=now - timedelta(seconds=20),
        ended_at=now,
        bytes=path.stat().st_size,
        sha256=sha256_file(path),
        created_at=now,
        expires_at=now + timedelta(days=30),
    )
)
cfg = CloudConfig(enabled=True, endpoint=endpoint, max_upload_mb=max_mb)
result = ClipUploader(store, cfg, token, SystemClock()).upload_next()
print(f"{result} {store.cloud_state(clip_id)}")
store.close()
sys.stdout.flush()
os._exit(0)
