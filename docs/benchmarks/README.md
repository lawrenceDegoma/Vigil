# Benchmarks

Raw JSON reports from `vigil bench`. Name files `YYYY-MM-DD-<host>-<kind>.json` and never compare numbers across hosts without saying so.

| Date | Host | Kind | Summary |
|---|---|---|---|
| 2026-11-15 | Apple M4 Pro (dev Mac) | offline | Default settings about 0.6 ms/frame p50, about 1.4 % of one core at 15 fps. Variant ordering is noisy on this machine (performance/efficiency core scheduling). **Reference only — not representative of a Pi.** |
| 2026-11-16 | Raspberry Pi 3 B+ (Pi OS on Debian 13 "Trixie", Python 3.13.5, OpenCV 4.10) | offline | Default 320 px with flow: p50 11.6 ms, p95 35.2 ms, max 142 ms; about 32 % of one core at 15 fps. Flow off: p95 19.4 ms. 160 px with flow: p95 16.9 ms. Synthetic frames, so analysis cost only. |
| 2026-11-16 | Raspberry Pi 3 B+ (same) | camera, 300 s | See the gate assessment below. |

## Phase 2 gate assessment, Pi 3 B+, 2026-11-16 (first run)

Criteria are from `docs/PI_SETUP.md` §4. The camera run used the default config: 1280x720 main stream with hardware H.264, 320x180 lores for analysis, 15 fps, flow on. The Pi was in its case with no fan.

| Measure | Target | Measured | Result |
|---|---|---|---|
| Delivered fps | ≥ 14.5 | 14.84 | pass |
| Dropped frames | < 1 % | 36 of about 4,500 (0.8 %) | pass, close to the limit |
| Analysis ms/frame p95 | < 40 ms | 22.3 ms (mean 16.2, max 92.9) | pass |
| Process CPU | < 60 % of one core | mean 65.7 %, p95 79 % | **miss** |
| Peak temperature | < 75 °C and `throttled = 0x0` | 58.0 °C; `0x80000` | **miss** (flag) |
| Peak RSS | < 250 MB | 164 MB | pass |
| Test clip | plays, has lead-in, framing OK | 588 frames, so about 39 s with the 30 s lead-in, but the file played at **2× speed** | **fixed in code; re-test** |

Notes:
- **CPU.** Analysis itself is about 24 % of one core (16.2 ms of the 66.7 ms frame budget). The other ~40 % is capture, Picamera2 and encoder-output handling in the same process. The Pi has 4 cores, so this is about 16 % of the machine. Frame rate and drops passed. The criterion is still missed as written; whether to keep it is an owner decision. Turning flow off would save about 8 ms/frame, but it would change the features the detector uses, so it is not recommended to meet the target.
- **Throttle flag.** `0x80000` means the soft temperature limit (60 °C on the 3 B+, which drops the clock from 1.4 to 1.2 GHz) was hit at some point since boot. It wasn't necessarily hit during this run: the camera run peaked at 58 °C at its start and settled at 47–50 °C. The likely cause is the multi-threaded offline benchmark (170–230 % CPU) run just before it, or the install. A stick-on heatsink is recommended. The 24 h soak decides.
- **Clip speed.** The ffmpeg remux used `-framerate`, which the stream's timing header overrides (it reports twice the rate). Fixed by using input `-r`, with a regression test. Reproduced and verified on the Mac against this clip's own H.264 stream: corrected duration 39.2 s. It has not yet been re-tested on the Pi.
