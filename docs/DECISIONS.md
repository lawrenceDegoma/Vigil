# Vigil — Architecture Decision Log

Short records of decisions that the design docs left open or that resolve conflicts between documents. Newest at the bottom. Status is one of `accepted`, `proposed` (needs owner input), or `superseded`.

---

### ADR-001 — Project name and subject-agnostic core
**Status:** accepted (2026-11-14)
The project is **Vigil**. Core code uses generic terms (*subject*, *profile*, *signal type*, *detector*, *severity*) so the same software can monitor other subjects (e.g. an elderly relative) later. Dog-specific wording and thresholds live in the `dog` profile and config, not in code paths. See DESIGN §1.1.

### ADR-002 — Phase numbering follows the owner's implementation brief
**Status:** accepted (2026-11-14)
DESIGN §12 lists 7 phases (0–6); the implementation brief lists 6 (0–5). We use the brief's numbering. The rule-based incident state machine (DESIGN "Phase 3 — Incident engine") is built in Phase 1 because offline replay and evaluation need it. Event clip capture from a live circular buffer stays in Phase 2.

### ADR-003 — Edge service runs natively under systemd, not in Docker
**Status:** accepted (2026-11-14)
Picamera2/libcamera are installed from Raspberry Pi OS apt packages and need device access (`/dev/video*`, `/dev/media*`, DMA heaps). Containerizing them on a 1 GB Pi 3 B+ adds memory overhead and a fragile device-passthrough setup. The edge service runs in a Python venv created with `--system-site-packages` (so `python3-picamera2` is visible) under a systemd unit with `Restart=always` and a watchdog. Docker Compose is reserved for services where it fits (local Supabase stack for development, optional cloud-side helpers). The edge code has no Pi-only imports at module load, so the same package runs on macOS and a future Pi 5 / mini PC.

### ADR-004 — Configuration format: TOML, strictly validated
**Status:** accepted (2026-11-14)
Edge configuration is TOML parsed by the standard library (`tomllib`, Python ≥ 3.11), so the Pi needs no extra parser. Config maps onto frozen dataclasses; unknown keys are errors (catches typos in safety-relevant thresholds). Secrets never go in TOML; they come from environment variables (`.env` locally, systemd `EnvironmentFile` on the Pi).

### ADR-005 — Detector modes: `off` / `shadow` / `alert`
**Status:** accepted (2026-11-14)
Every detector runs in one of three modes. `shadow` computes and logs scores and candidate detections but never opens incidents or notifies. Defaults for the dog profile: convulsion = `alert`, vomiting = `shadow` (experimental; motion-only heuristics are expected to confuse retching with panting, licking, and eating). Seizure detection never depends on the vomiting detector. Promoting vomiting to `alert` is an admin setting the owner can choose after reviewing shadow results.

### ADR-006 — Motion heuristics before neural networks
**Status:** accepted (2026-11-14)
Phase 1 uses frame differencing, global-motion estimation, sparse/dense optical flow, and spectral periodicity on low-resolution grayscale frames. "Localization" means tracking the dominant moving region, not recognizing a dog. A lightweight detector (e.g. MobileNet-SSD, YOLO-nano) is evaluated only after the Pi 3 benchmark in Phase 2 shows headroom. No classifier is trained on the single available seizure clip.

### ADR-007 — Python ≥ 3.11, uv for dependency management
**Status:** accepted (2026-11-14)
Raspberry Pi OS Bookworm ships Python 3.11, so 3.11 is the floor and the dev venv is pinned to 3.11 to catch incompatibilities early. `uv` manages the dev environment with a committed lockfile. Runtime dependencies are deliberately few: `numpy`, `opencv-python-headless`. Plotting (`matplotlib`) is an optional extra used only on the Mac.

### ADR-008 — Time handling
**Status:** accepted (2026-11-14)
The analysis pipeline works in **stream offsets** (seconds since the stream started, from frame timestamps), which makes file replay deterministic and keeps live operation immune to wall-clock jumps (NTP corrections). Wall-clock UTC is attached only when building outbound contract messages, from a base time recorded at stream start. All contract timestamps are RFC 3339 UTC.

### ADR-009 — One incident per continuous episode
**Status:** accepted (2026-11-14)
A signal must persist for `onset_seconds` (with short gaps tolerated) before an incident opens. Resumption within `merge_gap_seconds` after the signal stops continues the *same* incident. A convulsion that resumes after the merge gap opens a *new* incident linked to the previous one (`follows_incident_id`), because repeated seizures are clinically significant and must not be swallowed by dedup. Protection against notification storms (rate limits, reminder scheduling) lives in the notifier (Phase 3), not in detection.

### ADR-010 — Remote clip delivery
**Status:** proposed — owner decision needed before Phase 3
Options: (a) secure outbound tunnel (e.g. Tailscale or Cloudflare Tunnel) with an authenticated clip gateway on the Pi; (b) encrypted, event-only upload of incident clips to private Supabase Storage with short-lived signed URLs. (b) is simpler for family members and survives the Pi going offline, at the cost of incident clips leaving the house. (a) keeps footage local but fails when the Pi is unreachable. A recommendation will be made with cost estimates at the start of Phase 3.
