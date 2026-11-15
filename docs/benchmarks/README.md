# Benchmarks

Raw JSON reports from `vigil bench`. Name files `YYYY-MM-DD-<host>-<kind>.json` and never compare numbers across hosts without saying so.

| Date | Host | Kind | Summary |
|---|---|---|---|
| 2026-11-15 | Apple M4 Pro (dev Mac) | offline | Default settings about 0.6 ms/frame p50, about 1.4 % of one core at 15 fps. Variant ordering is noisy on this machine (performance/efficiency core scheduling). **Reference only — not representative of a Pi.** |
| — | Raspberry Pi 3 B+ | offline + camera | **Not yet measured** — Phase 2 gate (see docs/PI_SETUP.md §4). |
