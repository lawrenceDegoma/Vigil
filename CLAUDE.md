# CLAUDE.md — Vigil

## Mission
Implement [`docs/DESIGN.md`](docs/DESIGN.md) as a privacy-first, experimental **monitoring aid**. The first deployment watches one dog for possible seizures and possible vomiting; the core is subject-agnostic so it could later watch other subjects (e.g. an elderly relative, DESIGN §1.1). Vigil is not a medical device or a substitute for veterinary or medical guidance. Read `docs/DESIGN.md` and [`docs/DECISIONS.md`](docs/DECISIONS.md) before changing code. Never infer that a detected event is a confirmed seizure, or that vomiting reliably predicts one.

## Current stage
- Design accepted by the owner on 2026-11-14 (implementation authorized).
- **Phase 0 — foundation: done.** Repo layout, contracts, config, core interfaces.
- **Phase 1 — offline detection prototype: in progress.**
- Hardware is not assembled; the Pi is not set up. All development runs on the owner's Apple Silicon Mac against video files and synthetic scenarios.

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
    sources/               # FrameSource interface + backends
    detectors/             # Detector interface + per-signal detectors
  tests/
services/backend/          # Phase 3
supabase/                  # Phase 3 migrations/policies
apps/web/                  # Phase 4
scripts/                   # reproducible dev scripts
```

## Commands
```bash
make setup      # uv sync (Python 3.11) incl. plot extra
make check      # ruff + mypy --strict + pytest
make test-fast  # skip slow end-to-end tests
```

## Conventions
- Python ≥ 3.11 (Pi OS Bookworm). `uv` with committed `uv.lock`. Runtime deps: numpy, opencv-python-headless only; keep it that way unless justified in an ADR.
- `ruff` (lint + format, line length 100) and `mypy --strict` must pass.
- Pipeline stages are separate, independently testable components behind small interfaces; no stage imports a Pi-only module at import time (lazy-import Picamera2).
- Analysis time = stream offsets (seconds); UTC only at contract boundaries (ADR-008).
- Config: TOML, unknown keys rejected, every threshold documented as an experimental default (ADR-004). Detection-relevant settings feed `rule_version`.
- Detectors run in `off`/`shadow`/`alert` mode (ADR-005). Vomiting defaults to `shadow`.
- Generic vocabulary in core code: subject, profile, signal type, detector, severity. Dog-specific text lives in `profiles.py`/config.
- Record non-obvious decisions as ADRs in `docs/DECISIONS.md`.
- Commit each completed phase separately; push to `origin` (github.com/lawrenceDegoma/Vigil).

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
