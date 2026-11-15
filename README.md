# Vigil

**Vigil** is a local-first, privacy-preserving camera monitor that watches for possible seizure-like movement (and, experimentally, possible vomiting) and alerts a family. The first deployment watches a dog using a Raspberry Pi and camera; the core is subject-agnostic so it can later serve other cases, such as checking that an elderly relative hasn't collapsed.

> ⚠️ **Experimental monitoring aid, not a medical device.** Vigil can miss events and can raise false alarms. Its scores are uncalibrated engineering signals, not diagnoses. Keep a veterinarian- or clinician-approved response plan and human supervision.

## Status
| Phase | Scope | State |
|---|---|---|
| 0 | Foundation: layout, contracts, config, interfaces | ✅ done |
| 1 | Offline detection prototype (Mac) | 🚧 in progress |
| 2 | Raspberry Pi camera integration + benchmark | planned |
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
Requirements: [uv](https://docs.astral.sh/uv/) (`brew install uv`). uv installs Python 3.11 if needed.

```bash
make setup   # create services/edge/.venv with dev + plot dependencies
make check   # lint, strict type check, tests
```

Secrets: copy `.env.example` to `.env` for local values. Never commit `.env`, footage, or phone numbers.

## Documentation
- [docs/DESIGN.md](docs/DESIGN.md): requirements and architecture
- [docs/DECISIONS.md](docs/DECISIONS.md): architecture decision records
- [CLAUDE.md](CLAUDE.md): engineering rules and conventions
