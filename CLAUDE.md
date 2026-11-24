# CLAUDE.md — Vigil

## Mission
Implement [`docs/DESIGN.md`](docs/DESIGN.md) as a privacy-first, experimental **monitoring aid**. The first deployment watches one dog for possible seizures and possible vomiting; the core is subject-agnostic so it could later watch other subjects (e.g. an elderly relative, DESIGN §1.1). Vigil is not a medical device or a substitute for veterinary or medical guidance. Read `docs/DESIGN.md` and [`docs/DECISIONS.md`](docs/DECISIONS.md) before changing code. Never infer that a detected event is a confirmed seizure, or that vomiting reliably predicts one.

## Current stage
- Design accepted by the owner on 2026-11-14 (implementation authorized).
- **Phase 0 — foundation: done.** Repo layout, contracts, config, core interfaces.
- **Phase 1 — offline detection prototype: done** (validated on synthetic footage only — see `docs/DETECTION.md` §5 and ADR-011).
- **Phase 2 — Pi integration: on hardware; gate partly met.** Picamera2 dual-stream source + hardware-encoder circular clip backend, software ring recorder, clip manager, SQLite store/outbox, retention, health/heartbeats, systemd unit + watchdog, maintenance mode, `vigil run|bench|clips|health|maintenance`, installer (`deploy/pi/`), `docs/PI_SETUP.md`. First Pi 3 B+ run 2026-11-16 (Pi OS Trixie, Python 3.13, OpenCV 4.10): camera, install and benchmarks work. Results and gate table: `docs/benchmarks/README.md`. Passed: 14.84 fps, 0.8 % drops, p95 22 ms, 164 MB. Missed: process CPU 66 % (target 60 %; owner decision), throttle flag 0x80000 (heatsink advised). Clip remux played at 2×: fixed (input `-r`) and confirmed on the Pi. Clip finalization stalled capture by 1.5–2.5 s; moved to a worker thread. 300 s re-bench: 14.97 fps, 0 drops, but scene-dependent cost (p95 47 ms, CPU 78 %) and the throttle flag set again; the Phase 2 benchmark gate passes under the owner-accepted ADR-018 criteria; 40 ms / 60 % / throttle are soak warnings. SD card replaced and Pi re-flashed 2026-11-19. Camera mounted in its final position; dog measured at ~54 px uncropped, so deployment uses the analysis crop (ADR-017 note: 640x360 lores, 384 px, dog ~98 px). Crop bench passes ADR-018 (p95 29.5 ms, 0 drops) but the Pi sits about 63 °C soft-throttled with heatsinks and no fan, so airflow is needed before a pilot. Remaining: fan/airflow, measure the dog on the tiled spot, PI_SETUP §5–6 service run, 24 h soak and fault checklist.
- **Phase 3 — backend: in progress.** Hosted Supabase project (Free plan) has migrations 0100–0500 and all four functions deployed, with **mock SMS only**, and the cron job is calling dispatch (2026-11-20). The project ref lives only in gitignored `supabase/.temp/`. Owner decisions 2026-11-20: ADR-010 (b) event-only clip upload; ADR-019 no live phone view; US numbers. Done: Supabase schema/RLS/engine (`supabase/`), Edge Functions `device-ingest` + `dispatch`, mock SMS (Twilio gated by `SMS_LIVE_APPROVED`), edge `sync.py` + `[cloud]` config (off by default), ADR-020. Gate evidence: 63 pgTAP + 50 Deno unit tests; `make sms-dry-run` passes end to end, including the real Python pipeline through local functions. Clip upload/view done (ADR-010 implementation note; `clip_upload.py`, `clip-upload`/`clip-url`; 83 pgTAP, 57 Deno unit, e2e 15 steps). Remaining: device provisioning, hosted deploy + Twilio/toll-free verification (need owner approval and accounts), enable sync on the Pi after the soak.
- **Soak 1** (benchmarks README): 23 h 21 min, ended early by the owner. Hard gate held (0 drops, ≥14.94 fps, 0 restarts or errors). Warnings: soft-throttled at about 60–66 °C most of the day, CPU 51–70 %, small memory rise (+2 %). 0 convulsion incidents; 79 vomiting shadow detections producing about 415 MB/day of shadow clips.
- **Pi connected to the cloud 2026-11-20 23:19 PST:** device `pi-livingroom` registered (token hash only; token in the Pi's vigil.env, 0640 root:vigil), `[cloud]` enabled, status **online** in Supabase. SMS is still the mock provider; Twilio toll-free verification (sole proprietor) submitted and pending.
- **First deployed false alert** (DETECTION §5.4): 2026-11-20 23:36, the owner doing push-ups in the box raised an urgent convulsion incident (mock SMS only), now marked false_positive. Push-ups had about 1.5 Hz rhythm against about 3.25 Hz in the real seizure; ADR-021 adopts `slow_rhythm_hz = 1.8`, evaluated on all labelled footage. Rules version: deployed crop config `b0826d99`, **running on the Pi since 2026-11-21 00:12 PST** (owner-run restart). Also added: `vigil test-alert` / `send_test_alert()` (ADR-020 semantics).
- **Second deployed false alert** (DETECTION §5.5): 2026-11-21 13:30, the dog scratching with a hind leg (marked false_positive). Fixed by **ADR-022**: multi-limb evidence (rhythm's spatial spread) keeps the 10 s onset; one-spot rhythm needs 20 s. Evaluated: real seizure still 10.5 s; 0 deployed false alerts; synthetic 5/5. Rules: crop config `334bf782`, **running on the Pi since 2026-11-21 23:54 PST**; camera benchmark run 6 passes (p95 24.8 ms, 0 drops).
- **Soak 2** (benchmarks README): 23 h 35 min with the cloud connected, stopped by the owner to deploy ADR-022. Hard gate held (0 drops, ≥ 14.95 fps, 0 restarts or errors); same thermal and CPU warnings. One false urgent alert (the scratching, above). Still 25 min short of a strict 24 h.
- **Soak 3** (benchmarks README): **≥ 24 h requirement met** (2026-11-21 23:54 PST onward, still running at 61 h; 0 drops, 0 restarts, healthy in every heartbeat; soft-throttled, peak 67.7 °C). But **6 false urgent alerts in 61 h** under ADR-022 (DETECTION §5.7: sitting scratches close to the camera, one belly rub, one lying scratch). **ADR-023: onset 25 s** (both tiers), removes all 6 in replay; real seizure latency 25.4 s. **Running on the Pi since 2026-11-24 13:05 PST** (rules `d200d123`; config backup `vigil.toml.bak-2026-11-24-onset10`).
- All 8 deployed convulsion incidents (2026-11-20 to 2026-11-24) are owner-confirmed false positives and marked `false_positive` in Supabase.
- **Next:** watch for false alerts under ADR-023 and the §6 fault checklist; add a fan; shadow clips **turned off on the Pi 2026-11-24** (`recording.record_shadow = false`; detections still logged) until the vomiting detector is improved; 203 existing shadow clips (1.8 GB) expire under normal retention; enable Twilio after approval and the owner's go-ahead; evaluate owner's fixed-vantage negative footage (ADR-013 work); then Phase 3.
- The Pi (`vigil-pi`) is set up with the camera; Vigil is installed but the service hasn't been run for real yet. The owner's ~3 min handheld recording of a real episode is in gitignored `data/private/` with a provisional label file (0–68 s convulsion, visual estimate, awaiting owner confirmation). Three short negative behavior clips (petting/belly rub, chest scratch, chewing) are also there. Findings: `docs/DETECTION.md` §5.1–5.2. Key open problem: per-window motion features can't separate chewing/petting/scratch-reflex from the episode; only duration does. Owner domain knowledge (ADR-013): family steps back during an episode (human contact ⇒ evidence against), and episode paddling is multi-limb vs the one-leg scratch reflex. Fixed-vantage negatives (IMG_3052/3053: resting, petting, walk-through) analyzed in DETECTION §5.3: no convulsion false alerts; but at the wide framing the subject is only ~60 px and episode detection would be marginal → ADR-017 (≥100–120 px on subject; `preprocess.crop`). Scale experiments: `scripts/experiments/scale_transfer.py`. Never commit, upload, or describe identifying details of this footage.

## Phases (owner's numbering — ADR-002)
| Phase | Scope | Gate |
|---|---|---|
| 0 | Repo, contracts, config, interfaces, dev workflow | checks pass |
| 1 | Offline CV pipeline on video/synthetic input, incident FSM, CLI, evaluation harness | reproducible replay + baseline metrics, known failure cases |
| 2 | Picamera2 capture, circular buffer + event clips, systemd, health, Pi 3 benchmark | stable capture, bounded disk, measured FPS/CPU/temp |
| 3 | Supabase auth/RLS/migrations, device ingestion, SMS fan-out + reminders (mock first) | permission tests + SMS dry run pass |
| 4 | React dashboard: health, incidents, clips, acknowledgment, admin settings | responsive, auth-enforced |
| 5 | Fault injection, security tests, validation metrics, pilot | documented limitations, owner go/no-go |

## Fixed requirements — do not silently change
- Continuous 24/7 monitoring with automatic reboot recovery and visible maintenance mode.
- Arducam Camera Module 3 + Raspberry Pi 3 B+ as initial target; do not assume a Pi 3 can run a heavy model. Code must also run on a Pi 5 / mini PC without rewrites.
- Local-first video analysis; no continuous indoor video uploads.
- Independent possible-vomiting early warning and suspected-convulsion urgent alert. Seizure detection must never depend on vomiting detection.
- All authorized family members receive event alerts concurrently.
- One or two SMS reminder rounds for unacknowledged urgent incidents; no automatic calls.
- **Only the administrator** receives monitoring-offline notifications; no daily status messages.
- Family can view authorized clips, acknowledge, respond, resolve; **only admin** can edit settings, download, or preserve clips.
- Event-only recording with pre/post buffers; retention expiry plus disk quota; admin-preserved clips exempt from ordinary expiry.
- Dashboard shows honest `online/degraded/offline/maintenance/unknown` status from fresh heartbeats.
- Mobile-friendly React website for MVP, not native apps. Ring integration stays out of the critical path.

## Repository layout
```text
CLAUDE.md                  # this file
Makefile                   # make setup / check / test / demo
config/                    # example TOML configs (no secrets)
docs/                      # DESIGN.md, DECISIONS.md (ADRs), DETECTION.md
packages/contracts/        # versioned JSON Schemas (incident-event.v1, heartbeat.v1)
services/edge/             # Python edge service (uv project, src layout)
  src/vigil_edge/
    models.py              # immutable in-process data model + enums
    config.py              # strict TOML → dataclass config, rule_version hash
    profiles.py            # subject profiles: severity map + alert wording
    clock.py               # injectable clocks (System/Fake)
    sources/               # FrameSource: video_file, synthetic, live (webcam/paced/looping), picamera2_source (+ hardware clip backend)
    preprocess.py          # resize/grey/blur/ROI mask
    motion.py              # per-frame features: camera motion, diff masks (fast + lagged), region, flow
    windowing.py           # sliding-window features: persistence, spectral rhythm, travel
    detectors/             # Detector base + convulsion (alert) + vomiting (experimental, shadow)
    incidents.py           # incident state machine (persistence, dedup, escalation, recovery)
    pipeline.py            # orchestration, observers, per-stage timing
    outputs.py overlay.py plotting.py   # CSV/JSONL/summary, debug video, timeline PNG
    evaluation.py          # labels + event-level metrics (recall, false alerts, latency)
    contracts.py           # IncidentEvent → incident-event.v1 message
    recording.py           # clip backends (software ring) + ClipManager (pre/post buffer, split, hash)
    store.py               # SQLite: clip index + durable outbox (idempotent, bounded)
    sync.py                # CloudSync: outbox -> device-ingest (at-least-once, backoff, parking; ADR-020)
    clip_upload.py         # ClipUploader: alerted-incident clips -> storage, split into parts (ADR-010)
    retention.py           # pure retention planner (expiry, quota, free-space reserve) + executor
    health.py system.py systemd.py   # heartbeats, host metrics, sd_notify/watchdog
    service.py             # `vigil run`: sessions, reconnect backoff, housekeeping thread, maintenance
    bench.py               # offline + live-camera benchmarks
    cli.py                 # `vigil analyze | evaluate | synth | config | run | bench | clips | health | maintenance`
  tests/                   # unit + integration (synthetic end-to-end); helpers.py builders
supabase/                  # Phase 3 backend (local stack via pinned CLI in package.json; Docker)
  migrations/              # schema, RLS/grants + RPCs, private.* ingestion/notification engine
  functions/               # Deno Edge Functions: device-ingest, dispatch; _shared/ logic + unit tests
  tests/database/          # pgTAP: permissions + engine; tests/e2e/: SMS dry run
apps/web/                  # Phase 4
deploy/pi/                 # install.sh, vigil-edge.service (Type=notify, watchdog, hardened), journald limits
scripts/                   # reproducible dev scripts
```

## Commands
```bash
make setup      # uv sync (Python 3.11) incl. plot extra
make check      # ruff + mypy --strict + pytest (~40 s)
make test-fast  # skip slow end-to-end tests
make demo       # synthetic day → analyze (plot+overlay) → synthetic evaluation
make analyze VIDEO=path ARGS="--plot --overlay"   # any video; handheld: -c config/handheld.toml
uv run --project services/edge vigil evaluate --synthetic   # regression baseline
uv run --project services/edge vigil config --set key=value # effective config + rule_version
make simulate   # live service on looping synthetic input (data/edge-sim)
make bench      # offline analysis benchmark (on the Pi: see docs/PI_SETUP.md)
make pi-compat  # full tests on Debian Bookworm arm64 + apt OpenCV 4.6/NumPy 1.24 (Docker) — run before Pi deploys
make backend-setup   # npm ci (pinned supabase CLI 2.122.1 + deno 2.9.6) + local Supabase stack
make backend-check   # deno fmt/lint/check + unit tests + pgTAP database tests
make sms-dry-run     # Phase 3 gate: end-to-end alerts/reminders/offline with the MOCK provider
```

## Conventions
- Python ≥ 3.11 (Pi OS Bookworm). `uv` with committed `uv.lock`. Runtime deps: numpy, opencv-python-headless only; keep it that way unless justified in an ADR.
- `ruff` (lint + format, line length 100) and `mypy --strict` must pass.
- Pipeline stages are separate, independently testable components behind small interfaces; no stage imports a Pi-only module at import time (lazy-import Picamera2).
- Analysis time = stream offsets (seconds); UTC only at contract boundaries (ADR-008).
- Config: TOML, unknown keys rejected, every threshold documented as an experimental default (ADR-004). Detection-relevant settings feed `rule_version`.
- Detectors run in `off`/`shadow`/`alert` mode (ADR-005). Vomiting defaults to `shadow`.
- Generic vocabulary in core code: subject, profile, signal type, detector, severity. Dog-specific text lives in `profiles.py`/config.
- Detectors are stateless window scorers; all temporal logic (persistence, dedup, escalation) lives in `incidents.py`. A candidate confirms only on an *active* window.
- Features are resolution-independent (fractions of frame, frame-widths/s) so thresholds survive resolution/fps changes.
- When changing detection defaults: update `config/vigil.example.toml` (a test enforces it matches defaults), re-run `vigil evaluate --synthetic`, and record the change + data used in an ADR. Synthetic results are regression baselines, never accuracy claims.
- Synthetic scenario tests guard behaviour (no incidents on normal activity; one incident per episode; repeat episodes linked). Don't "fix" a failing scenario test by loosening it without understanding the behaviour change.
- Record non-obvious decisions as ADRs in `docs/DECISIONS.md`.
- Commit each completed phase separately; push to `origin` (github.com/lawrenceDegoma/Vigil).
- Pi runtime deps come from apt (picamera2, OpenCV, NumPy); never let pip replace them (ADR-014). Keep code compatible with NumPy ≥ 1.24 / OpenCV ≥ 4.6.
- Picamera2/libcamera are imported lazily and only in `sources/picamera2_source.py`; test it with fakes. Never hot-plug the CSI camera.
- Service invariants (ADR-015): camera failure ends the session and closes incidents (`stream_end`); watchdog fed only on capture progress or deliberate backoff; maintenance exports nothing; outbox never drops incident events.

## Safety and privacy rules
- Never upload raw living-room video, sample footage, phone numbers, or secrets to third-party services without explicit approval.
- Never commit credentials, `.env`, private clips, or raw recordings (`data/`, `runs/`, video extensions are gitignored). `.env.example` holds placeholders only.
- Least-privilege RLS and server-side authorization; frontend role checks are UX only.
- Family viewing restrictions cannot stop screen recording; do not claim otherwise.
- TLS, short-lived signed access, audit logging, per-device credentials, rate limiting, bounded local queues. No public inbound Pi ports.
- A Pi heartbeat alone does not prove camera/detector health.
- Alerts say **possible vomiting** or **possible seizure-like movement**, never a diagnosis. No automatic treatment advice; emergency guidance must be vet-approved.
- Do not send real SMS or deploy paid cloud services without owner approval. Never fabricate test results, accuracy, or benchmarks.

## Required tests (cumulative)
- Unit: motion features, FSM transitions, cooldown/dedup, retention expiry, disk quota, authorization policies, reminder scheduler, stale heartbeat logic.
- Integration: offline queue replay, device API auth, role enforcement, SMS dry-run fan-out, incident sync, secure clip retrieval.
- Fault injection: power/network loss, camera disconnect, low disk, duplicate/reordered events, delayed SMS provider, Pi restart.
- Evaluation: false alerts per day, detection latency, event-level recall on labeled footage; report dataset size and limitations.
- Soak: ≥ 24 h capture/analysis before any unattended pilot.

## Agent response format
For each implementation task report: **scope**, **design decisions**, **files changed**, **tests run and results**, **security/privacy impact**, **known limitations**, **next gate**. Ask a focused question when a requirement is ambiguous and the answer materially changes scope, security, cost, or functionality.
