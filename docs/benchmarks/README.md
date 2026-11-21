# Benchmarks

Raw JSON reports from `vigil bench`. Name files `YYYY-MM-DD-<host>-<kind>.json` and never compare numbers across hosts without saying so.

| Date | Host | Kind | Summary |
|---|---|---|---|
| 2026-11-15 | Apple M4 Pro (dev Mac) | offline | Default settings about 0.6 ms/frame p50, about 1.4 % of one core at 15 fps. Variant ordering is noisy on this machine (performance/efficiency core scheduling). **Reference only — not representative of a Pi.** |
| 2026-11-16 | Raspberry Pi 3 B+ (Pi OS on Debian 13 "Trixie", Python 3.13.5, OpenCV 4.10) | offline | Default 320 px with flow: p50 11.6 ms, p95 35.2 ms, max 142 ms; about 32 % of one core at 15 fps. Flow off: p95 19.4 ms. 160 px with flow: p95 16.9 ms. Synthetic frames, so analysis cost only. |
| 2026-11-16 | Raspberry Pi 3 B+ (same) | camera, 300 s | See the gate assessment below. |
| 2026-11-16 | Raspberry Pi 3 B+ (same), after reboot | camera, 60 s (`-camera-2`) | Clip-speed fix re-test: clip has 528 frames at 15 fps, lasts 35.2 s and plays at the right speed. 14.18 fps, 38 dropped frames (4.2 %), p95 39.3 ms, CPU mean 73 %, peak 45.1 °C, `throttled = 0x0`. Drops traced to the clip finish stalling capture (below). |
| 2026-11-17 (UTC) | Raspberry Pi 3 B+ (same), with the clip finish moved off the capture thread | camera, 300 s (`-camera-3`) | **14.97 fps, 0 dropped frames**, no sampler gaps; clip finished on the worker in 1.38 s. Per-frame cost was higher than in run 1: mean 26.1 ms, p95 47.1 ms, max 83.6 ms; CPU mean 78 %, near 100 % for 0–50 s, 141–147 s and 242–300 s. Peak 58.0 °C, still rising at the end; `throttled = 0x80000`. |

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


### Run 3 (2026-11-17 UTC): after the clip-finalization fix

| Measure | Target | Run 1 | Run 3 | Result (run 3) |
|---|---|---|---|---|
| Delivered fps | ≥ 14.5 | 14.84 | 14.97 | pass |
| Dropped frames | < 1 % | 0.8 % | 0 | pass |
| Analysis ms/frame p95 | < 40 ms | 22.3 | 47.1 | **miss** (budget 66.7 ms) |
| Process CPU (mean) | < 60 % of one core | 65.7 % | 77.6 % | **miss** |
| Peak temperature / flag | < 75 °C, `0x0` | 58.0 °C / `0x80000` | 58.0 °C / `0x80000` | **miss** (flag) |
| Peak RSS | < 250 MB | 164 MB | 167 MB | pass |
| Test clip | plays, lead-in | 2× speed | correct, finish 1.38 s off-thread | pass |

- **The drops were fixed.** No frames dropped across 300 s, including while the clip was being finished.
- **Analysis cost depends on the scene.** The motion stage averaged 24.1 ms per frame, compared with 14.6 ms in run 1 on the same hardware and config. The owner confirmed that they moved the camera and walked in front of it at the start, which matches the first high-CPU stretch. Moving the camera itself is heavier than anything a fixed mount sees. Per-frame motion work (feature tracking, RANSAC, connected components) grows with how much changes. A real episode is the high-motion case, so the heavier figure is the one that matters for headroom. Even so, p95 used 71 % of the frame budget and nothing dropped.
- **Heat.** The temperature rose steadily from 42.9 °C to 58.0 °C and had not levelled off. The soft-limit flag was set again since boot, which included an install. A heatsink is now recommended before the 24 h soak.
- **Open owner decision.** Make the hard gate "≥ 14.5 fps, < 1 % drops, p95 below the 66.7 ms frame budget", and keep 40 ms / 60 % as soak warnings? Or keep the current targets and buy margin by lowering fps, resolution or flow, which would require re-validating detection?

### Runs 4–5 (2026-11-20 UTC): owner's deployment framing with the analysis crop (ADR-017)

The camera is in its final position. Config: `camera.analysis_size = [640, 360]`, `preprocess.crop = [0.14, 0.28, 0.66, 0.57]` (white floor, cot and carpet), `preprocess.width = 384`, giving a 384x187 analysis frame (71,808 px, against 57,600 at the default 320x180). The dog on the cot is about 98 px long, against about 54 px without the crop. Rules `motion-rules-0.1.0+f9171d04`. The Pi was in its case with heatsinks and no fan, and about 63 °C throughout both runs.

| Measure | Run 4 (`-crop`) | Run 5 (`-crop-2`, bilinear for small downscales) | Gate (ADR-018) |
|---|---|---|---|
| Delivered fps / dropped | 14.97 / 0 | 14.97 / 0 | pass |
| Preprocess stage | 7.5 ms | 1.9 ms | — |
| Analysis ms/frame p95 (max) | 48.5 (65.9) | 29.5 (55.0) | pass; run 5 also under the 40 ms warning |
| Process CPU mean | 86 % | 68 % | warning (> 60 %) |
| Temperature / `throttled` | 63.9 °C max / `0x80008` | 64.5 °C max / `0x80008` | **warning**: soft limit (60 °C, 1.4→1.2 GHz) active during the runs |
| Peak RSS | 167 MB | 167 MB | pass |
| Test clip finish (off-thread) | 1.03 s | 1.26 s | pass |

The gate passes with the Pi soft-throttled. Per ADR-018 the remaining warnings are CPU and temperature. Heatsinks are already fitted, so the next step before an unattended pilot is airflow: a small 5 V fan, or a better-ventilated position. Raising `temp_soft_limit` instead is not recommended (Raspberry Pi documents possible instability). Runs 4–5 had less motion in view than run 3, so p95 under heavy motion will be higher. Confirm in the soak.

## Soak 1 (2026-11-19 23:53 to 2026-11-20 23:14 PST): 23 h 21 min, ended early by owner decision

Deployed config (crop, 640x360 lores, 384 px; rules `f9171d04`), camera in its final position, heatsinks and no fan, no cloud connection. Source: 1,399 queued heartbeats plus the service journal. The owner ended the run 39 minutes short of 24 h to connect the cloud, so this does **not** satisfy "≥ 24 h before an unattended pilot"; the next continuous run counts for that.

| Hour (PST) | fps min | temp avg / max °C | CPU avg % | mem used % | dropped |
|---|---|---|---|---|---|
| 00h | 14.97 | 62.2 / 63.4 | 67 | 28.3 | 0 |
| 02–05h | 14.98 | 60.0–60.3 / 61.2 | 51–52 | 28.6–28.9 | 0 |
| 07h | 14.95 | 62.4 / 64.5 | 69 | 28.7 | 0 |
| 12h | 14.98 | 62.4 / 62.8 | 64 | 28.7 | 0 |
| 18h | 14.95 | 64.2 / **66.6** | **70** | 29.6 | 0 |
| 22h | 14.97 | 64.3 / 65.5 | 68 | 30.0 | 0 |

- **Hard gate (ADR-018) held all day:** fps never below 14.94 in any hour; **0 dropped frames**; 0 restarts, 0 errors, 0 camera or session faults; process RSS 174 MB at the end.
- **Warnings:**
  - **Temperature:** at or above 60 °C almost the whole day, so the 3 B+ ran soft-throttled at 1.2 GHz most of the time (peak 66.6 °C in the evening).
  - **CPU:** above 60 % during the day and evening.

  A fan or better airflow is needed before a pilot.
- **Memory:** system memory used rose from 28.1 % to 30.1 % (about +18 MB), mostly in the evening. Not conclusive; watch for growth over a longer run.
- **Detections:**
  - **Convulsion:** 0 incidents, so 0 false urgent alerts in a day of ordinary household life. One day is not a false-alert rate.
  - **Vomiting (shadow mode):** 79 detections and 50 shadow clips (about 415 MB, roughly 8 MB each). That is disk churn on the SD card; the detector stays in shadow, and its clips should be reviewed or disabled (owner decision).
- **Disk:** clips 415 MB (shadow only), database 1 MB, root filesystem 10 % used.
- **Not yet done:** PI_SETUP §6 fault checklist (reboot, power loss, no camera, kill, low disk, maintenance).
