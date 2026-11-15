# Vigil

**Vigil** is a local-first, privacy-preserving camera monitor that watches for possible seizure-like movement (and, experimentally, possible vomiting) and alerts a family. The first deployment watches a dog using a Raspberry Pi and camera; the core is subject-agnostic so it can later serve other cases, such as checking that an elderly relative hasn't collapsed.

> ⚠️ **Experimental monitoring aid, not a medical device.** Vigil can miss events and can raise false alarms. Its scores are uncalibrated engineering signals, not diagnoses. Keep a veterinarian- or clinician-approved response plan and human supervision.

## Status
| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: layout, contracts, config, interfaces | ✅ done |
| 1 | Offline detection prototype (Mac) | ✅ done (synthetic-validated only) |
| 2 | Raspberry Pi camera integration + benchmark | next |
| 3 | Supabase backend + SMS notifications | planned |
| 4 | Web dashboard | planned |
| 5 | Reliability, security, validation | planned |

## Repository
```
config/              example configuration (TOML, no secrets)
docs/                design spec, decision log, detection notes
packages/contracts/  versioned JSON Schemas for device↔cloud messages
services/edge/       Python edge service (capture, analysis, incidents)
services/backend/    server-side functions (Phase 3)
supabase/            migrations + RLS policies (Phase 3)
apps/web/            React dashboard (Phase 4)
```

## Development setup (macOS, Apple Silicon)
Requirements: [uv](https://docs.astral.sh/uv/) (`brew install uv`). uv installs Python 3.11 if needed. No camera or Pi is required.

```bash
make setup   # create services/edge/.venv with dev + plot dependencies
make check   # ruff lint/format, mypy --strict, pytest (≈40 s)
make demo    # synthetic "day" → analysis with plot + overlay → synthetic evaluation
```

Secrets: copy `.env.example` to `.env` for local values. Never commit `.env`, footage, or phone numbers. `data/` and `runs/` are gitignored.

## Using the offline analyzer
The `vigil` CLI runs from the repo root via `uv run --project services/edge vigil …` (or `make analyze`).

```bash
# Analyze a video; writes runs/<name>-<timestamp>/{summary.json,events.jsonl,windows.csv,timeline.png,overlay.mp4}
make analyze VIDEO=data/private/clip.mp4 ARGS="--plot --overlay"

# Handheld phone footage needs camera stabilization
make analyze VIDEO=data/private/phone.mov ARGS="-c config/handheld.toml --plot --overlay"

# Synthetic scenarios (no footage needed)
uv run --project services/edge vigil synth --list
uv run --project services/edge vigil analyze synthetic:convulsion --plot

# Tune any threshold from the command line
uv run --project services/edge vigil analyze clip.mp4 --set detectors.convulsion.onset_seconds=12

# Event-level metrics on labeled footage (<video>.labels.json next to each video)
uv run --project services/edge vigil evaluate data/private/
uv run --project services/edge vigil evaluate --synthetic
```

`summary.json` records the `rule_version` (rule set + hash of every detection setting), the incidents, shadow detections, per-stage timing, and the effective config. Outputs contain numeric features only, except `overlay.mp4`, which is footage and stays local.

See [docs/DETECTION.md](docs/DETECTION.md) for how detection works, current synthetic results, known limitations, and how to analyze and label real recordings.

## Documentation
- [docs/DESIGN.md](docs/DESIGN.md): requirements and architecture
- [docs/DECISIONS.md](docs/DECISIONS.md): architecture decision records
- [docs/DETECTION.md](docs/DETECTION.md): detection approach, results, limitations
- [CLAUDE.md](CLAUDE.md): engineering rules and conventions
