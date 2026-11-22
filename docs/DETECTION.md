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

## 5. Results so far

`vigil evaluate --synthetic` with default config, `motion-rules-0.1.0+c1e1f72e`, measured 2026-11-14 on an Apple M4 Pro:

| Scenario set | Footage | Convulsion | Vomiting (shadow) |
|---|---|---|---|
| 12 fixed-camera scenarios (rest, walk, scratch, shake-off, lighting, camera shake/bump, person, retch, cluster, pause, mixed) | 19.0 min | 5/5 labeled episodes detected, 0 false alerts, latency 10.1–10.4 s | 1/1 detected, 0 false |
| `handheld` with `config/handheld.toml` | 1.7 min | 1/1, 0 false | — |
| `handheld` with fixed-camera defaults | 1.7 min | 1/1 | **2 false** (hand sway read as slow heaving) |

### 5.1 First real recording (owner's footage, 2026-11-14)

One owner-provided recording of a real episode was analyzed **locally only**. The footage and its label file stay in gitignored `data/private/`.

**Footage:** handheld phone video, about 2:55, outdoors on a walk. Close-up, with the subject on a leash and lying on its side. Recording starts *mid-episode*. Paddling is visible from the first frame to about 68 s. After that the subject lies still, gets up around 84 s, and the rest is post-episode walking and lying with the phone moving. **Label:** convulsion 0–68 s. This is a provisional visual estimate and not yet owner-confirmed.

Results with `config/handheld.toml` (`motion-rules-0.1.0+74e55392`):

| Window segment | Activity (median) | Flow fw/s | Rhythm | Dominant Hz | Camera fw/s | Windows active |
|---|---|---|---|---|---|---|
| Episode 5–68 s | 0.101 | 0.48 | 0.36 | mostly 2.5–4 | 0.17 | 94 % |
| Still / getting up 68–85 s | 0.058 | 0.31 | 0.38 | 1.1 | 0.31 | 91 % |
| Phone moving, sidewalk 85–130 s | 0.013 | 0.17 | 0.38 | 1.4 | 0.29 | 22 % |
| Post-episode walk/lie 130–175 s | 0.006 | 0.09 | 0.46 | 2.4 | 0.09 | 19 % |

- **Detected:** one urgent incident confirmed **10.0 s after recording start**. True onset preceded the recording, so latency from real onset can't be measured. Requiring rhythm (`rhythm_floor=0`) still detects it at 10.5 s; `onset_seconds=15` detects at 14.8 s.
- **Real rhythm is less clean than synthetic.** Spectral concentration has a median of 0.36, versus about 0.7 on synthetic shaking. Detection was carried mostly by persistence and intensity. This supports not making rhythm a hard requirement.
- **Fixed-camera defaults miss this clip (0/1)** because hand motion exceeds `max_camera_motion`. That's expected; handheld footage needs `config/handheld.toml`.
- **The incident ran long:** it ended at 85 s, then resumed at 124 s within the merge gap and ended at 129 s. The overlay shows the post-episode activity is mainly the **swinging leash** in the foreground, plus the subject getting up and residual camera motion. This is a handheld-on-a-walk artifact that a fixed indoor camera wouldn't have. Episode-end timing from this clip is therefore not meaningful.
- **Scale differs from deployment.** At this close range the moving region covers about 57 % of the frame, and a fixed living-room camera will see far smaller numbers. **No thresholds were changed based on this clip.** It shows that a real episode produces a strong, sustained response. It says nothing about false alarms in a fixed indoor setting.
- With stabilization on, textured footage analyzed at 174 fps on the Mac. The Pi will run without stabilization (fixed mount).

### 5.2 Owner-provided ordinary-behavior clips (2026-11-14)

Three short handheld indoor clips (5, 10 and 15 s) of behaviors the owner expects could cause false alarms. All are labeled negative, with labels in `data/private/`:

1. Belly rub by a human hand, rolling and twisting, then head scratches (15 s).
2. Chest scratch in dim light, then licking or nuzzling (5 s).
3. Chewing a bone while lying down and being petted (10 s).

All used `config/handheld.toml`. **No incidents or shadow detections.** That result is uninformative on its own, because every clip is at or below the 10 s persistence requirement. The window-level comparison is the useful part:

| Clip | Windows active | Median score | Longest active run | Dominant Hz |
|---|---|---|---|---|
| Real episode 0–68 s | 94 % | 0.89 | 62.4 s | 3.3 |
| Chewing + petting | **100 %** | 0.79 | 5.3 s (whole analyzable clip) | 1.2 |
| Belly rub + rolling | 40 % | 0.77 | 2.1 s (broken by lighting/locomotion suppressions) | 2.9 |
| Chest scratch | 0 % | 0.49 | — | 3.0 |

**Finding:** per-window motion features do **not** separate these behaviors from the real episode. **Duration is the only thing that does**, so chewing or vigorous petting lasting over about 10 s would very likely produce a false urgent alert with the current rules. Restricting the rhythm band to 2–6 Hz (the episode is 97 % ≥ 2 Hz; chewing peaks around 1.2 Hz) barely helps: chewing is still 91 % active, because intensity and persistence carry the score. The synthetic regression set is unchanged either way. **Defaults were not changed.** One positive and three short negatives can't justify tuning, and all four are handheld close-ups unlike the deployment view.

Directions this points to, to be evaluated on fixed-camera footage before adoption:
- **Human contact context: accepted direction (ADR-013).** The owner confirmed that family steps back during an episode, and that the scratch-reflex leg kick (the closest look-alike) happens while he is being scratched. Contact windows will pause convulsion evidence. This needs a person or hand detector whose Pi 3 cost is unmeasured (Phase 2 benchmark).
- **Limb and whole-body extent.** Episode paddling involves multiple limbs; the scratch reflex is typically one hind leg.
- **Spatial pattern.** The episode involves whole-body motion with paddling limbs. Chewing is concentrated at the head. With a fixed camera, a background model (e.g. MOG2) can segment the resting subject, and "fraction of the subject's silhouette moving" could separate localized from whole-body motion.
- **Posture.** Side-lying during the episode versus lying on the chest while chewing needs a pose or shape model. Deferred (ADR-006).

### 5.3 First fixed-vantage footage (2026-11-15)

Two owner clips from a propped phone at roughly the intended camera position. The view is a wide living-room shot (4K, apparently the phone's ultra-wide lens), with the subject in the lower-centre resting area, partly at the bottom edge. Both are labeled negative, with behavior annotations, in `data/private/`:
- **A (52 s):** a person leaves, then the subject rests awake.
- **B (78 s):** a person kneels and scratches and pets the subject for about 47 s, leaves, the subject lies on its side, and a person walks through.

**Default fixed-camera config:** no convulsion incidents. Petting barely registers: activity 0.0002, flow 0.010 fw/s, 0 % of windows active. There was 1 vomiting shadow false detection, as the person arrived to pet.

**Sensitivity at this scale (scale-transfer experiment, `scripts/experiments/scale_transfer.py`).** The subject is only about 60 px long at 320 px analysis width. The real-episode recording was re-analyzed at a width giving the subject the same pixel length, and its motion converted to deployment-frame units:

| Subject length | Episode activity / flow | Handheld-residue baseline (still subject, moving phone) | Read |
|---|---|---|---|
| 60 px (current wide view) | 0.0076 / 0.145 | 0.0021 / 0.105 | **marginal**: activity barely above the ramp floor, and most of the flow could be handheld residue |
| 90 px | 0.022 / 0.17 | 0.008 / 0.13 | better |
| 120 px | 0.044 / 0.24 | 0.018 / 0.17 | clear: activity alone saturates intensity |

Conclusion: in the current wide framing, detection of a real episode would be **marginal**. Petting is easily rejected, but partly because *everything* is small. Aim for **≥ 100–120 px of subject length** in the analysis frame (ADR-017).

**With an analysis crop** (`preprocess.crop = [0.2, 0.55, 0.55, 0.45]`, about 1.8× the pixels on the resting area): still no convulsion incidents. Petting reaches window scores of 0.40 at most (threshold 0.5); activity 0.0022 and flow 0.034 are 10–20× below the episode at the equivalent scale, and arm movement often triggers the locomotion suppression. People entering and leaving stay suppressed as locomotion. The **vomiting shadow detector becomes noisy (4 false detections in 2 min)**, mostly slow petting strokes, so it stays in shadow mode.

**Projection to the actual camera:** the owner's Arducam IMX708 (66° horizontal × 41° vertical) sees about the central half (linearly) of this phone view, so from the same spot the subject would be about 110–120 px long without any crop. The cropped run above approximates that view, which is the encouraging case, *if* the camera is aimed down at the resting area (PI_SETUP §2).

**Caveats:** two short clips, from one placement and one lighting condition; the transfer experiment is an approximation; no episode has been recorded from the fixed view, and none should be staged. The Pi pilot (shadow-style review of every clip) is what will establish false alerts per day.

**What this does and doesn't show.** The synthetic scenes were designed alongside the rules and some thresholds were calibrated on them (ADR-011), so these numbers are regression baselines, not accuracy estimates. They show that the plumbing works:

- persistence filters short rhythmic behaviors;
- travel filters locomotion;
- camera and lighting suppressions trigger;
- dedup, repeat linking and escalation behave as designed;
- latency is about `onset_seconds`.

Real footage will be harder.

**Speed (Mac, not Pi).** About 980 analysis fps on a 640×360 file at 15 fps analysis: motion ≈ 0.92 ms/frame, everything else < 0.1 ms. That is about 65× real time, excluding decode. **No Raspberry Pi measurement exists yet.** Phase 2 must benchmark capture + analysis + encoding on the Pi 3 B+ before any claim is made.

### 5.4 First false alert from the deployed camera (2026-11-20 23:36 PST)

This is the first real incident from the fixed Pi camera, using the deployed crop config (rules `f9171d04`), about 4 hours into the cloud-connected run. The incident was urgent, `convulsion`, peak 0.82, and lasted 27 s. Alerts and both reminder rounds went to the two household phones through the **mock** provider, and the clip uploaded. The owner reviewed it: **it was the owner doing push-ups inside the analysis box**. The incident was marked `false_positive`, and the clip and label are kept privately in `data/private/pi-incidents/`. Replaying the clip on the Mac reproduces the detection (peak 0.89).

| Feature (active windows) | Real seizure (§5.1, handheld; median, IQR) | Push-ups (fixed camera) |
|---|---|---|
| `dominant_hz` | **3.25** (2.52–3.75) | **1.52** (1.49–1.52) |
| `reversal_rate` (/s) | **6.8** (6.2–7.0) | **3.2** (3.0–3.6) |
| `low_periodicity` | 0.20 (0.14–0.31) | **0.61** (0.56–0.66) |
| `camera_motion` | (handheld, stabilised) | 0.005; the camera was steady |

Region area and flow magnitude are not comparable, because one recording is a handheld close-up and the other a far, fixed view. Rhythm frequency is scale-independent and separates these two cleanly. Paddling in the one real episode was about twice as fast as push-ups, and push-ups carry a strong, very regular slow component. **Caveats:**
- It is one seizure and one false alert.
- Rhythm in a seizure may slow towards its end.
- Slower clonic jerking may occur in other episodes.

**Adopted (ADR-021):** `slow_rhythm_hz = 1.8`. Evaluated on all labelled footage: removes this false alert, keeps the real seizure at the same latency (10.5 s) with no episode window suppressed, and leaves synthetic recall at 5/5. Originally considered: (currently `band_hz = [1.0, 6.0]`) towards about 2 Hz, or down-weight rhythm below about 2 Hz, or use strong `low_periodicity` as evidence against. Any of these needs an ADR and evaluation on all labelled footage (seizure, behaviour clips, fixed-vantage negatives, this false alert, synthetic set), plus the owner's knowledge of how fast paddling usually is. Human presence (ADR-013) is the other route.

### 5.5 Second deployed false alert: the dog scratching himself (2026-11-21 13:30 PST)

This came after ADR-021 was deployed (rules `b0826d99`). The incident was urgent, `convulsion`, peak 1.00, and lasted about 10 s, with alerts and reminders through the **mock** provider only. The clip was retrieved from private storage. The owner confirmed it: **the dog scratching himself with a hind leg**, with walking just before and after. The incident was marked `false_positive`, and the clip and label are kept privately in `data/private/pi-incidents/`.

| Feature (active windows) | Scratching (Pi) | Real seizure (§5.1) | Push-ups (§5.4) |
|---|---|---|---|
| `dominant_hz` | **3.8–4.0** | 3.25 | 1.5 |
| `periodicity` p50 / max | **0.84 / 0.89** | 0.35 / **0.62** | 0.46 |
| `reversal_rate` (/s) | 7.4–7.8 | 6.8 | 3.2 |
| active run | **about 10 s** | 68 s | about 35 s |

Rhythm speed does not separate scratching from paddling, which is why ADR-021 correctly did not apply. Two features do separate them:
- **Regularity.** Scratching was almost metronomic (≥ 0.84), while the real seizure never exceeded 0.62, and petting, chest-scratch and chewing clips stayed ≤ 0.51. But the synthetic convulsions are unrealistically regular (median 0.73–0.77), so a regularity cap would break the synthetic regression set. It also rests on one seizure and one scratching bout, so it is **not adopted**. Revisit with more real data, and make the synthetic generator less regular first, with its own ADR.
- **Duration** (`onset_seconds`), evaluated on all labelled footage:

| onset_seconds | Real seizure | Pi false alerts (push-ups, scratching) | Behaviour / fixed clips | Synthetic |
|---|---|---|---|---|
| 10 (current) | detected, **10.5 s** | 1 (scratching; push-ups already removed by ADR-021) | 0 / 0 | 5/5, 10.3 s |
| **15** | detected, **15.3 s** | **0** | 0 / 0 | 5/5, 15.2 s |
| 20 | detected, 20.6 s | 0 | 0 / 0 | 5/5, 20.2 s |

15 s is within the design's suggested range (DESIGN, 10–15 s) and removes this alert at the cost of about 5 s more alert latency. A scratching bout longer than about 15 s would still alert. Adopting it is the **owner's decision** (pending).

### 5.6 Multi-limb evidence: spatial spread of the rhythm (ADR-022)

The spread of rhythmic cells relative to the moving body's size separates one scratching leg from paddling limbs, on the data available:

| Clip (32x16 grid) | Spread p10 / p25 / median / max | Strong (≥ 0.8) scoring windows |
|---|---|---|
| Real seizure (handheld) | 0.43 / 1.15 / **1.58** / 2.29 | 65 % in the first 15 s, 81 % overall |
| Synthetic 4-leg paddling | 0.88 / 0.94 / **0.95** / 0.98 | 93 % |
| Dog scratching (Pi) | 0.52 / 0.63 / **0.68** / 1.15 | **0 %** |
| Push-ups (Pi) | 0.23 / 0.24 / 0.85 / 1.56 | 50 % (already suppressed by ADR-021) |
| Petting (fixed / close-up) | median 1.35 / 1.88 | 100 % (keeps the 10 s onset, as before) |

The first grid (16x8, about 24 px cells) and absolute extents did not separate them: the scratch spread 0.14 frame-widths against 0.20 for four-leg paddling, and handheld close-ups are not comparable to the Pi view. See ADR-022 for the rule and the evaluation.

## 6. Known limitations and failure modes

1. **Motion ≠ subject.** There is no dog or person recognition. Any sustained in-place rhythmic motion can trigger: a person exercising, a fan, a TV in view, curtains in wind, another pet. Mitigations: ROI polygon, camera placement, and later a lightweight detector if the Pi benchmark allows (ADR-006).
2. **Thresholds depend on subject size in frame.** Activity is a fraction of the ROI, so a small, distant subject produces small numbers. Flow speed partially compensates. Tighten the ROI around the usual resting area.
3. **Occlusion and out-of-frame events are missed.** A seizure behind furniture or out of view produces nothing.
4. **Low light.** No IR or night handling yet. Auto-exposure jumps are suppressed per window, which can delay detection by one window length.
5. **Tonic phases** (rigid, little motion) are invisible to motion analysis. Detection relies on the clonic (shaking/paddling) phase.
6. **Chewing, being petted or rubbed, long scratching, vigorous play in place, or grooming** lasting > 10 s will likely trigger. Owner clips show these are indistinguishable from the real episode per window (§5.2). This is the main expected false-alarm source; quantifying it needs longer fixed-camera footage.
7. **Handheld footage** requires `config/handheld.toml`. Stabilization can partially cancel subject motion when the subject fills most of the frame.
8. **Vomiting detection is weak.** Slow heaving is subtle, and it overlaps with panting and eating. It stays in shadow mode until real shadow-mode data justifies promotion.
9. **Latency floor ≈ `onset_seconds`** (10 s) by design, trading speed for fewer false alarms. DESIGN suggests 10–15 s.
10. Scores are **uncalibrated**. `confidence` in contracts is the peak score, not a probability.

## 7. Analyzing real footage (e.g. the recorded seizure)

**Recording ordinary-behavior footage for evaluation.** Prop the phone where the Pi camera will go (same height, angle and room; landscape; not handheld). Record clips of at least 1–2 minutes so the 10 s persistence rule is actually tested. Priority behaviors:
- scratch-reflex leg kicks while being scratched, side-lying *and* belly-up;
- belly rubs and petting;
- chewing a bone or toy;
- self-scratching and grooming;
- rolling;
- sleeping, including twitching in sleep;
- people walking through;
- lights switching.

Include the moment a person steps away. Label each clip with an empty `events` list, and optionally add behavior annotations, which the evaluator ignores but which support per-behavior analysis:
```json
{"events": [], "annotations": [{"behavior": "scratch_reflex", "start": 12.0, "end": 40.0, "human_contact": true}]}
```


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
