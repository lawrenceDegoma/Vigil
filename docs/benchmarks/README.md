# Benchmarks

Raw JSON reports from `vigil bench`. Name files `YYYY-MM-DD-<host>-<kind>.json` and never compare numbers across hosts without saying so.

| Date | Host | Kind | Summary |
|---|---|---|---|
| 2026-11-15 | Apple M4 Pro (dev Mac) | offline | Default settings about 0.6 ms/frame p50, about 1.4 % of one core at 15 fps. Variant ordering is noisy on this machine (performance/efficiency core scheduling). **Reference only — not representative of a Pi.** |
| 2026-11-16 | Raspberry Pi 3 B+ (Pi OS on Debian 13 "Trixie", Python 3.13.5, OpenCV 4.10) | offline | Default 320 px with flow: p50 11.6 ms, p95 35.2 ms, max 142 ms; about 32 % of one core at 15 fps. Flow off: p95 19.4 ms. 160 px with flow: p95 16.9 ms. Synthetic frames, so analysis cost only. |
| 2026-11-16 | Raspberry Pi 3 B+ (same) | camera, 300 s | See the gate assessment below. |
| 2026-11-16 | Raspberry Pi 3 B+ (same), after reboot | camera, 60 s (`-camera-2`) | Clip-speed fix re-test: clip has 528 frames at 15 fps, lasts 35.2 s and plays at the right speed. 14.18 fps, 38 dropped frames (4.2 %), p95 39.3 ms, CPU mean 73 %, peak 45.1 °C, `throttled = 0x0`. Drops traced to the clip finish stalling capture (below). |

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
- **Clip speed.** The ffmpeg remux used `-framerate`, which the stream's timing header overrides (it reports twice the rate). Fixed by using input `-r`, with a regression test. Reproduced and verified on the Mac against this clip's own H.264 stream: corrected duration 39.2 s. Re-tested on the Pi: correct (35.2 s, 528 frames).
- **Clip finalization stalled capture.** In both runs the 5 s CPU sampler shows a gap at the moment the test clip stopped: 6.5 s instead of 5 at t ≈ 110 s, and 7.5 s instead of 5 at t ≈ 30 s in the re-test. The ffmpeg remux, then the SHA-256 and the store insert, ran on the capture thread. The dropped frames (36 and 38) match a 1.5–2.5 s stall. In the service this would recur at every clip stop and at every split during a long episode. Fixed: the capture loop only stops the encoder, and the slow work runs on one background worker. The benchmark now does the same and reports `test_clip.finish_seconds`. Needs a Pi re-run to confirm drops fall below 1 %.
- **The 60 s re-test is less representative than the 300 s run.** Its first 10 s (camera start-up, ~100 % CPU) and the 10 s clip are a third of the run, which explains the higher mean ms/frame and CPU. Use 300 s runs to judge the gate.
