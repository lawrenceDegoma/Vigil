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
**Status:** accepted by the owner (2026-11-20): option **(b)**, event-only clip upload
Decision: the Pi uploads clips **only for alerted incidents** to a **private** Supabase Storage bucket. It uploads through a short-lived signed upload URL issued per clip by a device-authenticated function. Family members view clips only after login, through short-lived signed URLs issued by a server-side function that checks household membership. Shadow-mode clips, non-alerted clips and normal footage never leave the Pi. Encryption is TLS in transit plus the provider's at-rest storage encryption. It is **not end-to-end**: the provider could technically access uploaded clips. This is stated to the owner and in the dashboard. Cloud copies follow the same retention as the household setting; admin-preserved clips are exempt. Uploading can be turned off per device (`upload_clips = false`), which falls back to local-only clips.
*Implementation (2026-11-20):*
- Bucket and access:
  - The `incident-clips` bucket is private and has **no storage policies** for users, so nobody can list or read objects directly, not even admins.
  - `clip-url` returns 5-minute signed links, which are audited. Only admins get download links (`Content-Disposition: attachment`).
- Upload flow:
  - The device registers a clip, then PUTs each part to a signed upload URL, then calls `complete`. The server checks each part's size in storage before accepting it.
  - A clip becomes viewable only when every part has arrived. Cloud expiry runs from upload time, and preserved clips are exempt.
- Parts and playback:
  - Clips over `cloud.max_upload_mb` (default 45; the Free plan allows 50 MB per file) are cut into playable MP4 parts with ffmpeg stream copy (no re-encode).
  - Parts that are still too large are cut again.
  - The Pi remux adds `+faststart` so phones can start playing before the download finishes.
- Upload timing: uploads run in their own thread and wait until incident events are delivered, so they never delay alerts.
Original options: (a) secure outbound tunnel (e.g. Tailscale or Cloudflare Tunnel) with an authenticated clip gateway on the Pi; (b) encrypted, event-only upload of incident clips to private Supabase Storage with short-lived signed URLs. (b) is simpler for family members and survives the Pi going offline, at the cost of incident clips leaving the house. (a) keeps footage local but fails when the Pi is unreachable. A recommendation will be made with cost estimates at the start of Phase 3.

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
- **Recording.** The Pi uses dual streams: `main` 1280x720 goes to the hardware H.264 encoder and `CircularOutput` (`pre_event_seconds` lead-in); `lores` 320x180 YUV420 feeds analysis via its Y plane. Clips stop `post_event_seconds` after the last open incident ends, split at `max_clip_seconds`, and are remuxed to MP4 with `ffmpeg -r <fps> -c copy` (input `-r`, because the encoder's VUI timing makes `-framerate` produce 2× speed clips). The capture loop only stops the encoder; remux, SHA-256 and indexing run on one ordered background worker (inline finalization stalled capture by 1.5–2.5 s on the Pi 3). Shadow detections get short clips (pre-event + 30 s) so experimental detectors can be reviewed.
- **Watchdog.** `Type=notify` with `WatchdogSec=60`. The watchdog is fed only while the capture loop has processed a frame within 15 s, or is deliberately backing off, so a hung libcamera call triggers a restart but a missing camera does not cause a restart loop.
- **Maintenance.** A flag file `<data_dir>/state/maintenance`, toggled by `vigil maintenance on|off`. Analysis keeps running and logging; nothing is exported or recorded; heartbeats report `maintenance: true`.
- **Outbox.** Incident events and heartbeats are queued durably in SQLite (idempotent on `event_id`). The sender is Phase 3. The bound drops sent rows, then the oldest unsent heartbeats, and never incident events.
- **Autofocus.** Defaults to a single autofocus cycle at start. Continuous AF can hunt and appear as scene-wide motion.

### ADR-016 — Wall clock without an RTC
**Status:** proposed (2026-11-15) — implement in Phase 3
The Pi 3 has no real-time clock. After a boot without network, UTC can be wrong until NTP syncs. Detection is unaffected (monotonic stream offsets, ADR-008). Phase 3: include a `clock_synced` flag (from `timedatectl show -p NTPSynchronized`) in heartbeats and events, and have the cloud prefer its own receive time when the device clock is unsynced. *Observed 2026-11-19:* after a power-off move, the Pi booted with its clock about 8 minutes behind (the time saved at shutdown). NTP corrected it 42 s after boot. The first heartbeat (monotonic 30.9 s) carried the stale UTC time, and the next one (90.96 s, exactly 60 s later by monotonic time) appeared to be 8.5 minutes later in the log. Detection was unaffected.


### ADR-017 — Pixels on the subject: framing target and analysis crop
**Status:** accepted (2026-11-15)
The scale-transfer experiment (DETECTION §5.3) shows real episode motion becomes marginal when the subject is about 60 px long in the analysis frame, and clear at about 120 px. Target **≥ 100–120 px of subject length** at the analysis width. Means, in order of preference:
1. **Framing:** mount the camera so the usual resting areas fill the lower-middle of the frame, not the edge. Prefer the standard Camera Module 3 (about 66° horizontal field of view) over the wide variant unless coverage requires it. *Owner's camera (2026-11-15): Arducam IMX708, 75°D × 66°H × 41°V. That is about 0.5× the phone ultra-wide's linear field, so the subject appears about 2× larger (about 110–120 px) from the same position. Expected to meet the target without a crop, provided the camera is aimed down at the resting area. Verify with the PI_SETUP §2 measurement.*
2. **Analysis crop** `preprocess.crop` (new): crops the resting area *before* downscaling, so analysis cost is unchanged. On the Pi, raise `camera.analysis_size` (e.g. 640x360, scaled by the ISP at no CPU cost) so the crop has real resolution. Clips still record the full view.
3. A larger `preprocess.width`, only if the Pi benchmark shows headroom.
ROI polygons remain in full-frame coordinates.
*Owner's deployment (2026-11-19):* the mount can't be moved closer without losing one of its vantage points. Measured from a full-resolution still, the dog on his cot spans 16.8 % of the frame width, so about **54 px** at the default 320 px, which is too small. His usual spots are the cot, a tiled floor area to the left, and the carpet towards the camera. All three fit in one wide box: `crop = [0.14, 0.28, 0.66, 0.57]`, with `analysis_size = [640, 360]` and `width = 384`. That gives a 384x187 analysis frame with the dog at about **98 px** on the cot. The tiled spot is probably further away; estimated 80–90 px there, not yet measured. The owner chose coverage of the carpet over the extra detail of 422 px, which would have exceeded the Pi 3 frame budget. **Detection only covers the box.** The bottom-left of the box includes a seat, so human motion there is in view (see ADR-013). Benchmarked on the Pi in docs/benchmarks (runs 4–5): passes ADR-018.

### ADR-018 — Phase 2 performance gate: hard criteria and warning thresholds
**Status:** accepted by the owner (2026-11-16)
The original PI_SETUP §4 targets mixed real requirements with conservative margins I had chosen. On the Pi 3 B+, cost per frame depends on the scene: the motion stage took 14.6 ms per frame in a quiet run and 24.1 ms with the camera being moved and a person in view (docs/benchmarks, runs 1 and 3). A real episode is a high-motion case. Run 3 dropped no frames at 15 fps, but missed the 40 ms p95 and 60 % CPU margins.
- **Hard gate (pass/fail):** ≥ 14.5 fps delivered, < 1 % dropped frames, analysis p95 below the frame budget (66.7 ms at 15 fps), peak RSS < 250 MB, and a correct test clip. These must also hold across the 24 h soak.
- **Warning thresholds (watch during the soak, not blocking):** analysis p95 > 40 ms, process CPU > 60 % of one core, temperature ≥ 60 °C or a non-zero `throttled`. A sustained breach means acting before a pilot: a heatsink or better airflow first; then the cheaper analysis variants, which require re-validating detection.
- **Not chosen:** lowering fps, resolution or optical flow now to meet the margins. Each changes the features the detector sees and would need re-validation, which isn't justified while no frames are dropped.

### ADR-019 — No live camera view on phones (for now)
**Status:** accepted by the owner (2026-11-20)
A live view on phones stays out of scope (DESIGN §3, "unrestricted remote live stream"). On the Pi 3 it would add encoder/network load to a device already at its soft thermal limit (docs/benchmarks, runs 4–5). It would also mean on-demand living-room video leaving the house. Camera aiming uses the temporary LAN-only `rpicam-vid --listen` + `ffplay` method (PI_SETUP). Phones get incident clips (ADR-010) and device status. Revisit on stronger hardware, as a design change with its own ADR.

### ADR-020 — Device-to-cloud delivery and alert fan-out semantics
**Status:** accepted (2026-11-20)
- **Device to cloud.** The edge SQLite outbox is the source of truth. `CloudSync` (its own thread) posts batches to `device-ingest`, authenticated with a per-device token. The token is stored server-side only as a SHA-256 hash and on the device only in `/etc/vigil/vigil.env`. A row is marked sent only when the server answers `accepted` or `duplicate` for it, so delivery is **at-least-once**, made safe by server idempotency on `event_id`. Outages back off exponentially (5 s up to 300 s). Queuing an incident event wakes the sender immediately, even during backoff. A message the server rejects `max_rejections` times is **parked**: kept on the device and counted in `queue_depth`, but no longer retried, so a contract bug cannot block alerts queued behind it.
- **Server ingestion.** Ingestion tolerates reordering: any event type can create the incident; only newer events change mutable fields; severity and confidence only ratchet up. A device can only touch its own incidents.
- **Fan-out.** Each alert is queued once per distinct household phone number, then sent concurrently. Severity escalation alerts again. Freshness uses the cloud receive time (ADR-016).
- **Reminders.** Reminders follow **acknowledgement**, not episode end: any status other than `open` stops them, and they never apply to warnings. One or two rounds at a configurable interval (default 5 min).
- **Admin-only notices.** Offline and recovery notices go to the admin only, never during maintenance, and once per outage.
- **Rate cap.** An hourly per-household cap protects against message storms, but never applies to first urgent alerts.
- **Delivery to phones.** At-least-once with leases: a crash between sending and recording can produce a duplicate text, which is preferred over a missed alert.
- **Real SMS.** The mock provider is the default. Twilio refuses to start without `SMS_LIVE_APPROVED=yes` (owner approval). Logs mask phone numbers.

### ADR-021 — A clear rhythm slower than 1.8 Hz is evidence against a convulsion
**Status:** accepted (2026-11-21). Not yet deployed to the Pi; that needs a restart, at the owner's call.
**Context.** The first alert from the deployed camera was the owner doing push-ups in the analysis box (DETECTION §5.4). Compared with the one real episode, push-ups showed a dominant rhythm of about 1.5 Hz, against 3.25 Hz (IQR 2.5–3.75) for paddling. The owner reports that paddling stays fast for most of an episode and slows only near the end.
**Decision.** `detectors.convulsion.slow_rhythm_hz = 1.8` with `slow_rhythm_min_periodicity = 0.3`: a window whose motion is **clearly** rhythmic (periodicity ≥ 0.3) with a dominant frequency below 1.8 Hz is suppressed (`slow_rhythm`). Windows without a clear rhythm (tonic or irregular phases) are unaffected, and the rhythm floor still allows them to score.
**Evaluation (2026-11-21).** All labelled footage, at thresholds off / 1.8 / 2.0 / 2.2:

| Set | Off | 1.8 | 2.0 | 2.2 |
|---|---|---|---|---|
| Real seizure (handheld config) | detected, 10.5 s | detected, 10.5 s | detected, 10.5 s | detected, 10.5 s |
| Push-ups (deployed crop config) | **1 false alert** | 0 | 0 | 0 |
| Behaviour clips IMG_2246/4871/5362 | 0 FA | 0 | 0 | 0 |
| Fixed-vantage IMG_3052/3053 | 0 FA | 0 | 0 | 0 |
| Synthetic set (12 clips) | 5/5, 0 FA | 5/5, 0 FA | 5/5, 0 FA | **4/5** (missed `mixed`: 2.2 Hz ± 0.4 wander) |

At 1.8 and 2.0, none of the real seizure's 119 episode windows were suppressed. The incident now ends at 1:13 instead of 2:39 (the labelled episode ends at 1:08), because the slow post-episode handling no longer keeps it open. 1.8 was chosen over 2.0 for margin against slower episodes.
**Limits.**
- The evidence is one real seizure and one false alert.
- A genuinely slow (< 1.8 Hz), clearly periodic clonic episode would be suppressed.
- Faster exercise (≥ about 1.8 Hz) would still alert.
- The other route remains human presence (ADR-013).

The rules version changes: default `d1574146`, owner's deployed crop config `b0826d99`.
