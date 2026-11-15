"""Scale-transfer experiment: would a recorded episode be detectable at deployment scale?

Analyses an episode recording at a reduced width so the subject spans the same
number of pixels it would in the fixed deployment view (blur, thresholds and
morphology then act on the same pixel scale), then converts the per-frame
motion measurements into fractions of the deployment analysis frame and
re-scores them with the configured convulsion rules.

  implied_activity = moving_pixels / (deploy_w * deploy_h)
  implied_flow     = flow_px_per_s / deploy_w

Approximation: viewing angle, occlusion, lighting and the transfer of
handheld stabilization residue are not modelled. Compare settings with it;
don't treat it as accuracy.

Usage (repo root):
  services/edge/.venv/bin/python scripts/experiments/scale_transfer.py VIDEO \
      --end 68 --subject-px 60 --subject-frac 0.9 -c config/handheld.toml
"""

from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np

from vigil_edge.cli import build_config
from vigil_edge.detectors.base import ramp
from vigil_edge.pipeline import run_pipeline
from vigil_edge.sources.video_file import VideoFileSource


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, required=True, help="end of the labeled episode")
    ap.add_argument(
        "--subject-px",
        type=float,
        nargs="+",
        required=True,
        help="subject length in pixels in the deployment analysis frame",
    )
    ap.add_argument(
        "--subject-frac",
        type=float,
        required=True,
        help="subject length as a fraction of the episode video's width",
    )
    ap.add_argument("--deploy-size", default="320x180")
    ap.add_argument("-c", "--config", action="append", default=[])
    args = ap.parse_args()
    dw, dh = (int(v) for v in args.deploy_size.split("x"))
    base = build_config(args.config, [])
    c = base.detectors.convulsion
    print(
        f"{'subject px':>10} {'analysis w':>10} {'impl. activity':>14} {'impl. flow':>10} "
        f"{'windows active':>14} {'longest run':>11} {'incident':>8}"
    )
    for px in args.subject_px:
        width = round(px / args.subject_frac)
        if width < 64:  # minimum analysis width; smaller subjects can't be emulated
            print(f"{px:>10.0f}  skipped: needs analysis width {width} < 64")
            continue
        cfg = replace(
            base,
            preprocess=replace(base.preprocess, width=width),
            motion=replace(base.motion, flow_width=min(base.motion.flow_width, width)),
        )
        src = VideoFileSource(args.video, cfg.source.target_fps, args.start, args.end)
        h = round(src.frame_size[1] * width / src.frame_size[0])
        res = run_pipeline(src, cfg)
        k_area = (width * h) / (dw * dh)
        k_flow = width / dw
        ff = res.frame_features
        act = np.array([f.motion_fraction * k_area for f in ff])
        flow = np.array([f.flow_magnitude * k_flow for f in ff])
        ts = np.array([f.timestamp for f in ff])
        active_windows, runs, cur, last = [], 0.0, None, None
        for w in res.windows:
            sel = (ts >= w.features.t_start) & (ts <= w.features.t_end)
            fw = w.features
            persistence = ramp(
                float(np.mean(act[sel] > cfg.window.activity_floor)), *c.active_ratio_ramp
            )
            intensity = max(
                ramp(float(act[sel].mean()), *c.activity_ramp),
                ramp(float(flow[sel].mean()), *c.flow_ramp),
            )
            rhythm = ramp(fw.periodicity, *c.periodicity_ramp)
            score = persistence * intensity * (c.rhythm_floor + (1 - c.rhythm_floor) * rhythm)
            suppressed = (
                fw.travel > c.max_travel
                or fw.camera_motion > c.max_camera_motion
                or fw.lighting_change > c.max_lighting_change
            )
            on = score >= c.threshold and not suppressed
            active_windows.append(on)
            t = fw.t_end
            if on:
                if cur is None or (last is not None and t - last > 3.0):
                    cur = t
                last = t
                runs = max(runs, t - cur)
        frac = float(np.mean(active_windows)) if active_windows else 0.0
        print(
            f"{px:>10.0f} {width:>10d} {float(np.median(act)):>14.4f} "
            f"{float(np.median(flow)):>10.3f} {frac * 100:>13.0f}% {runs:>10.1f}s "
            f"{'likely' if runs >= c.onset_seconds else 'no':>8}"
        )


if __name__ == "__main__":
    main()
