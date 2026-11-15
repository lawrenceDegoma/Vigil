# Vigil — Detection Approach (Phase 1)

> **Status: experimental prototype.** Everything below describes engineering heuristics evaluated on *synthetic* footage only. No threshold here is medically validated, no accuracy figure applies to real animals or people, and the system can miss events or raise false alarms. Detections are *possible* events for a human to review.

## 1. Goal of Phase 1

Show that a lightweight, CPU-only motion pipeline can turn video into **candidate events** with measurable latency and false alerts, and make it easy to inspect *why* each candidate was or wasn't raised. Everything runs on recorded or synthetic video on the development Mac. The same code will run on the Raspberry Pi in Phase 2.

## 2. Pipeline

```
FrameSource → Preprocessor → MotionFeatureExtractor → WindowAnalyzer → Detectors → IncidentStateMachine → observers
 file/synth    320px grey,      per-frame features      5 s windows      per-signal   persistence, dedup,      CSV/JSONL,
 (Picamera2)   blur, ROI mask                           every 0.5 s      scores       escalation, recovery     plot, overlay
```

| Stage | Module | What it does |
|---|---|---|
| Acquisition | `sources/` | `VideoFileSource` subsamples to `source.target_fps` (default 15) on a fixed time grid; timestamps = position in file. `SyntheticSource` renders scripted scenarios with ground-truth labels. |
| Preprocess | `preprocess.py` | Downscale to `preprocess.width` (320), greyscale, Gaussian blur, optional normalized ROI polygon. |
| Motion | `motion.py` | (1) Camera-motion estimate: LK-tracked corners + RANSAC similarity transform. With `motion.stabilize` the previous frame is warped onto the current one. (2) Gain-compensated frame difference → motion mask → `motion_fraction`. (3) A second, *lagged* difference (`slow_lag_seconds`, 0.25 s) → `slow_motion_fraction` for slow heaving. (4) Dominant moving region (connected components) → centroid/bbox. (5) Dense Farneback flow at 160 px over moving pixels → mean direction and speed. |
| Window | `windowing.py` | Every 0.5 s over the last 5 s: activity and **active ratio** (persistence), **spectral concentration** of the oscillation signal in the convulsion band (1–6 Hz) and slow band (0.4–1.2 Hz), direction-reversal rate, **travel** of the moving region, region area, camera motion (p90), max lighting jump. |
| Detectors | `detectors/` | Stateless window scorers; one per signal type; `off` / `shadow` / `alert` mode. |
| Incidents | `incidents.py` | Persistence, gap tolerance, one-incident-per-episode, repeat-episode linking, early warning → urgent escalation, recovery observation. |

All features are resolution-independent: fractions of the frame, or frame widths per second.

**Rhythm measure.** For each window, the per-frame mean flow vectors are projected onto their principal axis. This gives a signed back-and-forth signal at the shaking frequency, alongside the detrended motion-fraction signal. Each is Hann-windowed and FFT'd. Spectral concentration is the power within one main lobe of the strongest in-band peak, divided by all power above 0.2 Hz. A pure oscillation scores close to 1; noise scores about 0.1–0.3.

## 3. Detectors

### Convulsion-like motion (`alert` by default)
Hypothesis: a convulsive episode in a fixed camera view looks like **persistent, vigorous, often rhythmic motion that stays in place**.

```
score = persistence × intensity × (rhythm_floor + (1 − rhythm_floor) × rhythm)
persistence = ramp(active_ratio)            intensity = max(ramp(activity), ramp(flow speed))
rhythm      = ramp(spectral concentration)  rhythm_floor = 0.35 (irregular jerking still counts, less)
```

Hard suppressions block a window outright:

| Suppression | Fixed camera | Handheld config |
|---|---|---|
| Camera motion, frame-widths/s | > 0.05 | > 1.5 |
| Lighting jump | > 0.06 | > 0.06 |
| Travel of moving region | > 0.10 | > 0.25 |
| Scene-wide motion area | > 0.6 | > 0.95 |

A window score ≥ 0.5 makes it *active*. Duration is enforced by the incident state machine: an incident opens only after activity has persisted `onset_seconds` (10 s). This is what separates sustained episodes from scratching (3–6 s) or shaking off water (about 2 s), which are both strongly rhythmic.

### Possible vomiting / retching (EXPERIMENTAL, `shadow` by default)
Hypothesis: retching shows as **slow (0.4–1.2 Hz), repeated heaving while stationary**. It uses the lagged "slow" motion channel because slow heaving barely changes between consecutive frames. `score = persistence × intensity × slow-band rhythm`. It is suppressed for vigorous motion (activity > 0.08 or flow > 0.08 fw/s), travel, camera motion and lighting. Onset 4 s. Expected confusions: panting, licking, eating or drinking, heavy breathing, sniffing. In shadow mode it only logs `shadow_detection` records and never opens incidents or alerts (ADR-005). Seizure detection never depends on it.

## 4. Incident state machine

- **Candidate → confirmation.** Active windows build a candidate. Gaps up to 3 s are tolerated. The candidate is confirmed on an *active* window once it has lasted `onset_seconds` from its estimated onset, with ≥ 60 % of its windows active. Onset is estimated as `first active window end − coverage × window length`, where coverage is the fraction of that window showing the detector's motion.
- **One incident per episode.** While open, every active window extends the incident. After 8 s of quiet it ends (`ended_at` = last active time).
- **Resumption vs repeat.** If the same signal is confirmed again within `merge_gap_seconds` (30 s) of the end, the same incident is resumed (`incident_updated`, `resumed`). After that, a **new** urgent incident opens with `follows_incident_id` pointing to the previous one. Repeated seizures must not be swallowed by dedup.
- **Warning signals** (vomiting) dedupe across the whole early-warning window (10 min) and put the system into `early_warning`. A later convulsion opens an urgent incident linked to the warning, and clears the warning, so recovery returns to `monitoring`.
- **States:** `monitoring` → `early_warning` → `urgent` → `recovery_observation` (120 s) → `monitoring`, derived each update (DESIGN §5.2).

Notification rate limiting and reminders belong to the Phase 3 notifier, not to detection.

## 5. Results so far (synthetic only)

`vigil evaluate --synthetic` with default config, `motion-rules-0.1.0+c1e1f72e`, measured 2026-11-14 on an Apple M4 Pro:

| Scenario set | Footage | Convulsion | Vomiting (shadow) |
|---|---|---|---|
| 12 fixed-camera scenarios (rest, walk, scratch, shake-off, lighting, camera shake/bump, person, retch, cluster, pause, mixed) | 19.0 min | 5/5 labeled episodes detected, 0 false alerts, latency 10.1–10.4 s | 1/1 detected, 0 false |
| `handheld` with `config/handheld.toml` | 1.7 min | 1/1, 0 false | — |
| `handheld` with fixed-camera defaults | 1.7 min | 1/1 | **2 false** (hand sway read as slow heaving) |

**What this does and doesn't show.** The synthetic scenes were designed alongside the rules and some thresholds were calibrated on them (ADR-011), so these numbers are regression baselines, not accuracy estimates. They show that the plumbing works:

- persistence filters short rhythmic behaviors;
- travel filters locomotion;
- camera and lighting suppressions trigger;
- dedup, repeat linking and escalation behave as designed;
- latency is about `onset_seconds`.

Real footage will be harder.

**Speed (Mac, not Pi).** About 980 analysis fps on a 640×360 file at 15 fps analysis: motion ≈ 0.92 ms/frame, everything else < 0.1 ms. That is about 65× real time, excluding decode. **No Raspberry Pi measurement exists yet.** Phase 2 must benchmark capture + analysis + encoding on the Pi 3 B+ before any claim is made.

## 6. Known limitations and failure modes

1. **Motion ≠ subject.** There is no dog or person recognition. Any sustained in-place rhythmic motion can trigger: a person exercising, a fan, a TV in view, curtains in wind, another pet. Mitigations: ROI polygon, camera placement, and later a lightweight detector if the Pi benchmark allows (ADR-006).
2. **Thresholds depend on subject size in frame.** Activity is a fraction of the ROI, so a small, distant subject produces small numbers. Flow speed partially compensates. Tighten the ROI around the usual resting area.
3. **Occlusion and out-of-frame events are missed.** A seizure behind furniture or out of view produces nothing.
4. **Low light.** No IR or night handling yet. Auto-exposure jumps are suppressed per window, which can delay detection by one window length.
5. **Tonic phases** (rigid, little motion) are invisible to motion analysis. Detection relies on the clonic (shaking/paddling) phase.
6. **Long scratching, vigorous play in place, or grooming** lasting > 10 s may trigger. This is the main expected false-alarm source; it needs real "normal day" footage to quantify.
7. **Handheld footage** requires `config/handheld.toml`. Stabilization can partially cancel subject motion when the subject fills most of the frame.
8. **Vomiting detection is weak.** Slow heaving is subtle, and it overlaps with panting and eating. It stays in shadow mode until real shadow-mode data justifies promotion.
9. **Latency floor ≈ `onset_seconds`** (10 s) by design, trading speed for fewer false alarms. DESIGN suggests 10–15 s.
10. Scores are **uncalibrated**. `confidence` in contracts is the peak score, not a probability.

## 7. Analyzing real footage (e.g. the recorded seizure)

Footage stays on the Mac. Put it under `data/`, which is gitignored, and never commit or upload it.

```bash
mkdir -p data/private
cp ~/Downloads/seizure.mov data/private/
# Phone footage is handheld → use the handheld overrides.
make analyze VIDEO=data/private/seizure.mov ARGS="-c config/handheld.toml --plot --overlay --frame-csv"
```

Then:
1. Open `runs/<name>-<timestamp>/timeline.png` and `overlay.mp4` to see activity, rhythm, scores and suppressions over time.
2. Write labels next to the video as `data/private/seizure.labels.json`:
   ```json
   {"events": [{"signal_type": "convulsion", "start": 12.0, "end": 85.0, "note": "owner-labeled onset/offset"}]}
   ```
   Re-run `analyze`: it picks up the labels and reports detection and latency for that clip.
3. Tune with `--set`, e.g. `--set detectors.convulsion.onset_seconds=12`. Every detection-relevant change alters `rule_version`.
4. Collect **negative** footage too (sleeping, scratching, play, people walking through, lights switching), ideally from the eventual fixed camera position. One positive clip can show the pipeline *can* respond to a real episode. It cannot establish thresholds or reliability, and no classifier will be trained on it.

`vigil evaluate data/private/` runs every video that has a `.labels.json` and reports event-level detection, false alerts per hour and latency for that dataset.
