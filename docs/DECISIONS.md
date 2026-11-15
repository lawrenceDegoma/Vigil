# Vigil — Architecture Decision Log

Short records of decisions that the design docs left open or that resolve conflicts between documents. Newest at the bottom. Status is one of `accepted`, `proposed` (needs owner input), or `superseded`.

---

### ADR-001 — Project name and subject-agnostic core
**Status:** accepted (2026-11-14)
The project is **Vigil**. Core code uses generic terms (*subject*, *profile*, *signal type*, *detector*, *severity*) so the same software can monitor other subjects (e.g. an elderly relative) later. Dog-specific wording and thresholds live in the `dog` profile and config, not in code paths. See DESIGN §1.1.

### ADR-002 — Phase numbering follows the owner's implementation brief
**Status:** accepted (2026-11-14)
DESIGN §12 lists 7 phases (0–6); the implementation brief lists 6 (0–5). We use the brief's numbering. The rule-based incident state machine (DESIGN "Phase 3 — Incident engine") is built in Phase 1 because offline replay and evaluation need it. Event clip capture from a live circular buffer stays in Phase 2.

### ADR-003 — Edge service runs natively under systemd, not in Docker
**Status:** accepted (2026-11-14)
Picamera2/libcamera are installed from Raspberry Pi OS apt packages and need device access (`/dev/video*`, `/dev/media*`, DMA heaps). Containerizing them on a 1 GB Pi 3 B+ adds memory overhead and a fragile device-passthrough setup. The edge service runs in a Python venv created with `--system-site-packages` (so `python3-picamera2` is visible) under a systemd unit with `Restart=always` and a watchdog. Docker Compose is reserved for services where it fits (local Supabase stack for development, optional cloud-side helpers). The edge code has no Pi-only imports at module load, so the same package runs on macOS and a future Pi 5 / mini PC.

### ADR-004 — Configuration format: TOML, strictly validated
**Status:** accepted (2026-11-14)
Edge configuration is TOML parsed by the standard library (`tomllib`, Python ≥ 3.11), so the Pi needs no extra parser. Config maps onto frozen dataclasses; unknown keys are errors (catches typos in safety-relevant thresholds). Secrets never go in TOML; they come from environment variables (`.env` locally, systemd `EnvironmentFile` on the Pi).

### ADR-005 — Detector modes: `off` / `shadow` / `alert`
**Status:** accepted (2026-11-14)
Every detector runs in one of three modes. `shadow` computes and logs scores and candidate detections but never opens incidents or notifies. Defaults for the dog profile: convulsion = `alert`, vomiting = `shadow` (experimental; motion-only heuristics are expected to confuse retching with panting, licking, and eating). Seizure detection never depends on the vomiting detector. Promoting vomiting to `alert` is an admin setting the owner can choose after reviewing shadow results.

### ADR-006 — Motion heuristics before neural networks
**Status:** accepted (2026-11-14)
Phase 1 uses frame differencing, global-motion estimation, sparse/dense optical flow, and spectral periodicity on low-resolution grayscale frames. "Localization" means tracking the dominant moving region, not recognizing a dog. A lightweight detector (e.g. MobileNet-SSD, YOLO-nano) is evaluated only after the Pi 3 benchmark in Phase 2 shows headroom. No classifier is trained on the single available seizure clip.

### ADR-007 — Python ≥ 3.11, uv for dependency management
**Status:** accepted (2026-11-14)
Raspberry Pi OS Bookworm ships Python 3.11, so 3.11 is the floor and the dev venv is pinned to 3.11 to catch incompatibilities early. `uv` manages the dev environment with a committed lockfile. Runtime dependencies are deliberately few: `numpy`, `opencv-python-headless`. Plotting (`matplotlib`) is an optional extra used only on the Mac.

### ADR-008 — Time handling
**Status:** accepted (2026-11-14)
The analysis pipeline works in **stream offsets** (seconds since the stream started, from frame timestamps), which makes file replay deterministic and keeps live operation immune to wall-clock jumps (NTP corrections). Wall-clock UTC is attached only when building outbound contract messages, from a base time recorded at stream start. All contract timestamps are RFC 3339 UTC.

### ADR-009 — One incident per continuous episode
**Status:** accepted (2026-11-14)
A signal must persist for `onset_seconds` (with short gaps tolerated) before an incident opens. Resumption within `merge_gap_seconds` after the signal stops continues the *same* incident. A convulsion that resumes after the merge gap opens a *new* incident linked to the previous one (`follows_incident_id`), because repeated seizures are clinically significant and must not be swallowed by dedup. Protection against notification storms (rate limits, reminder scheduling) lives in the notifier (Phase 3), not in detection.

### ADR-010 — Remote clip delivery
**Status:** proposed — owner decision needed before Phase 3
Options: (a) secure outbound tunnel (e.g. Tailscale or Cloudflare Tunnel) with an authenticated clip gateway on the Pi; (b) encrypted, event-only upload of incident clips to private Supabase Storage with short-lived signed URLs. (b) is simpler for family members and survives the Pi going offline, at the cost of incident clips leaving the house. (a) keeps footage local but fails when the Pi is unreachable. A recommendation will be made with cost estimates at the start of Phase 3.

### ADR-011 — Phase 1 thresholds were calibrated on synthetic footage
**Status:** accepted (2026-11-14)
During Phase 1 the following defaults were set by inspecting feature distributions on the synthetic scenarios: convulsion `max_travel` 0.2 → 0.1, `max_camera_motion` 0.35 → 0.05, `max_region_area` 0.9 → 0.6. The vomiting detector moved to the lagged "slow" motion channel, with ramps `activity` (0.001, 0.004), `active_ratio` (0.2, 0.5), `low_periodicity` (0.15, 0.4), a `max_flow` 0.08 suppression and threshold 0.35. Synthetic evaluation results are therefore **regression baselines, not accuracy estimates**. Real thresholds must come from the owner's footage (positive episode plus ordinary-day negatives) and be recorded here with the dataset used.

### ADR-012 — Lagged ("slow") motion channel
**Status:** accepted (2026-11-14)
At 15 fps, slow movements (abdominal heaving, slow rolling) move ~1 px between consecutive analysis frames and vanish under the difference threshold. A second difference against the frame `slow_lag_seconds` (0.25 s) earlier costs one extra `absdiff` per frame and makes these movements measurable. It is not camera-compensated, so with `stabilize` it falls back to the consecutive-frame value.

### ADR-013 — Human contact pauses convulsion evidence (owner domain knowledge)
**Status:** accepted direction (2026-11-14); implementation pending a person/hand detector benchmarked on the Pi. **Priority lowered 2026-11-15:** from the fixed vantage, 47 s of petting never came close to a convulsion alert (DETECTION §5.3). It does still fool the experimental vomiting detector. Revisit if the pilot shows contact-driven false alerts.
Owner input: during an episode the family steps back to give the dog space, so **a person touching the subject is evidence against a convulsion**. The main look-alike, a **scratch-reflex leg kick**, happens *while being scratched*, side-lying or belly-up. Petting, belly rubs and chewing while being petted also score like the episode per window (DETECTION §5.2).

Rule to implement:
- Windows where a person is in contact with, or immediately beside, the subject do **not** count toward a convulsion candidate. They pause it rather than reset it.
- Once contact ends, suspicious motion must persist the normal `onset_seconds` before an incident opens. A real episode (typically 60–90 s) therefore still alerts shortly after people step back.
- If a person is present but *not* touching the subject (standing back), evidence counts normally.
- Contact must never permanently disable detection, and the rule is a profile setting (it may not suit other subjects).

Complementary feature: **limb or whole-body extent of motion**. Episode paddling involves several limbs and the body; the scratch reflex is typically one hind leg. Measure it with a fixed camera via subject silhouette segmentation; evaluate on the owner's fixed-vantage footage before adoption.

### ADR-014 — Pi dependencies come from apt; Vigil installs with `--no-deps`
**Status:** accepted (2026-11-15)
`python3-picamera2` and its compiled helpers are built against Raspberry Pi OS's NumPy. Letting pip install NumPy 2.x or `opencv-python-headless` wheels into the venv can break Picamera2 at import time and pulls about 50 MB of duplicate libraries onto a 1 GB device. On the Pi the venv uses `--system-site-packages`, and Vigil is installed with `pip install --no-deps`, so NumPy, OpenCV and Picamera2 all come from apt. On the Mac, `uv` installs pinned wheels as before. The code must keep working with the older apt versions (NumPy ≥ 1.24, OpenCV ≥ 4.6).

### ADR-015 — Live service semantics
**Status:** accepted (2026-11-15)
- **Sessions.** A camera failure ends the *session*: open incidents are closed with `end_reason: stream_end` (exported, so the dashboard never shows a phantom ongoing incident), the clip in progress is saved, and the service reconnects with exponential backoff (1 s up to 60 s). State is never carried across a camera outage.
- **Recording.** The Pi uses dual streams: `main` 1280x720 goes to the hardware H.264 encoder and `CircularOutput` (`pre_event_seconds` lead-in); `lores` 320x180 YUV420 feeds analysis via its Y plane. Clips stop `post_event_seconds` after the last open incident ends, split at `max_clip_seconds`, and are remuxed to MP4 with `ffmpeg -c copy`. Shadow detections get short clips (pre-event + 30 s) so experimental detectors can be reviewed.
- **Watchdog.** `Type=notify` with `WatchdogSec=60`. The watchdog is fed only while the capture loop has processed a frame within 15 s, or is deliberately backing off, so a hung libcamera call triggers a restart but a missing camera does not cause a restart loop.
- **Maintenance.** A flag file `<data_dir>/state/maintenance`, toggled by `vigil maintenance on|off`. Analysis keeps running and logging; nothing is exported or recorded; heartbeats report `maintenance: true`.
- **Outbox.** Incident events and heartbeats are queued durably in SQLite (idempotent on `event_id`). The sender is Phase 3. The bound drops sent rows, then the oldest unsent heartbeats, and never incident events.
- **Autofocus.** Defaults to a single autofocus cycle at start. Continuous AF can hunt and appear as scene-wide motion.

### ADR-016 — Wall clock without an RTC
**Status:** proposed (2026-11-15) — implement in Phase 3
The Pi 3 has no real-time clock. After a boot without network, UTC can be wrong until NTP syncs. Detection is unaffected (monotonic stream offsets, ADR-008). Phase 3: include a `clock_synced` flag (from `timedatectl show -p NTPSynchronized`) in heartbeats and events, and have the cloud prefer its own receive time when the device clock is unsynced.


### ADR-017 — Pixels on the subject: framing target and analysis crop
**Status:** accepted (2026-11-15)
The scale-transfer experiment (DETECTION §5.3) shows real episode motion becomes marginal when the subject is about 60 px long in the analysis frame, and clear at about 120 px. Target **≥ 100–120 px of subject length** at the analysis width. Means, in order of preference:
1. **Framing:** mount the camera so the usual resting areas fill the lower-middle of the frame, not the edge. Prefer the standard Camera Module 3 (about 66° horizontal field of view) over the wide variant unless coverage requires it. *Owner's camera (2026-11-15): Arducam IMX708, 75°D × 66°H × 41°V. That is about 0.5× the phone ultra-wide's linear field, so the subject appears about 2× larger (about 110–120 px) from the same position. Expected to meet the target without a crop, provided the camera is aimed down at the resting area. Verify with the PI_SETUP §2 measurement.*
2. **Analysis crop** `preprocess.crop` (new): crops the resting area *before* downscaling, so analysis cost is unchanged. On the Pi, raise `camera.analysis_size` (e.g. 640x360, scaled by the ISP at no CPU cost) so the crop has real resolution. Clips still record the full view.
3. A larger `preprocess.width`, only if the Pi benchmark shows headroom.
ROI polygons remain in full-frame coordinates.
