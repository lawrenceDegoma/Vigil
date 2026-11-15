"""Observers that persist analysis output for retrospective review.

Outputs contain numeric features and timestamps only, never image data.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import IO, Any, TextIO

from vigil_edge import __version__
from vigil_edge.config import config_to_dict
from vigil_edge.incidents import Output
from vigil_edge.models import (
    FrameFeatures,
    IncidentEvent,
    ShadowDetection,
    StateChange,
    WindowFeatures,
)
from vigil_edge.pipeline import FrameContext, NullObserver, PipelineResult, WindowRecord


def fmt_time(seconds: float | None) -> str:
    """Format a stream offset as m:ss.s (or h:mm:ss.s)."""
    if seconds is None:
        return "-"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{sign}{int(h)}:{int(m):02d}:{s:04.1f}"
    return f"{sign}{int(m)}:{s:04.1f}"


def output_to_dict(out: Output) -> dict[str, Any]:
    if isinstance(out, IncidentEvent):
        inc = out.incident
        return {
            "kind": out.event_type.value,
            "at": round(out.at, 3),
            "event_id": out.event_id,
            "incident_id": inc.id,
            "signal_type": inc.signal_type.value,
            "severity": inc.severity.value,
            "started_at": round(inc.started_at, 3),
            "opened_at": round(inc.opened_at, 3),
            "ended_at": None if inc.ended_at is None else round(inc.ended_at, 3),
            "peak_score": round(inc.peak_score, 4),
            "follows_incident_id": inc.follows_incident_id,
            "rule_version": inc.rule_version,
            "features": {
                k: (round(v, 5) if isinstance(v, float) else v) for k, v in out.features.items()
            },
        }
    if isinstance(out, StateChange):
        return {
            "kind": "state_changed",
            "at": round(out.at, 3),
            "from": out.previous.value,
            "to": out.current.value,
            "reason": out.reason,
        }
    if isinstance(out, ShadowDetection):
        return {
            "kind": "shadow_detection",
            "at": round(out.at, 3),
            "detector": out.detector,
            "signal_type": out.signal_type.value,
            "started_at": round(out.started_at, 3),
            "peak_score": round(out.peak_score, 4),
        }
    raise TypeError(type(out))


class EventsJsonlWriter(NullObserver):
    """One JSON object per state-machine output."""

    def __init__(self, path: Path) -> None:
        self._fh: TextIO = path.open("w", encoding="utf-8")

    def on_output(self, output: Output) -> None:
        self._fh.write(json.dumps(output_to_dict(output)) + "\n")

    def close(self) -> None:
        self._fh.close()


_WINDOW_FIELDS = [f.name for f in fields(WindowFeatures)]
_FRAME_FIELDS = [f.name for f in fields(FrameFeatures) if f.name not in ("centroid", "region")]


class TimelineCsvWriter(NullObserver):
    """Per-window features, detector scores, and state, for plotting and tuning."""

    def __init__(self, path: Path, detector_names: list[str]) -> None:
        self._fh: IO[str] = path.open("w", newline="", encoding="utf-8")
        self._names = detector_names
        cols = ["state", *_WINDOW_FIELDS]
        for n in detector_names:
            cols += [f"{n}_score", f"{n}_active", f"{n}_suppressed"]
        self._writer = csv.writer(self._fh)
        self._writer.writerow(cols)

    def on_window(self, record: WindowRecord) -> None:
        row: list[Any] = [record.state.value]
        row += [round(getattr(record.features, k), 5) for k in _WINDOW_FIELDS]
        for n in self._names:
            r = record.result(n)
            if r is None:
                row += ["", "", ""]
            else:
                row += [r.score, int(r.active), "|".join(r.suppressed_by)]
        self._writer.writerow(row)

    def close(self) -> None:
        self._fh.close()


class FrameCsvWriter(NullObserver):
    """Per-frame motion features (larger; opt-in)."""

    def __init__(self, path: Path) -> None:
        self._fh: IO[str] = path.open("w", newline="", encoding="utf-8")
        self._writer = csv.writer(self._fh)
        self._writer.writerow([*_FRAME_FIELDS, "cx", "cy"])

    def on_frame(self, ctx: FrameContext) -> None:
        f = ctx.features
        if f is None:
            return
        row: list[Any] = [round(float(getattr(f, k)), 5) for k in _FRAME_FIELDS]
        row += [round(f.centroid[0], 4), round(f.centroid[1], 4)] if f.centroid else ["", ""]
        self._writer.writerow(row)

    def close(self) -> None:
        self._fh.close()


class ConsoleReporter(NullObserver):
    """Prints state-machine outputs as they happen."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._out = stream or sys.stderr

    def on_output(self, output: Output) -> None:
        d = output_to_dict(output)
        at = fmt_time(output.at)
        if isinstance(output, IncidentEvent):
            inc = output.incident
            extra = ""
            if output.event_type.value == "incident_opened":
                latency = inc.opened_at - inc.started_at
                extra = f" onset≈{fmt_time(inc.started_at)} (+{latency:.1f}s)"
            elif inc.ended_at is not None:
                extra = f" ended {fmt_time(inc.ended_at)}"
            print(
                f"[{at}] {d['kind']:<16} {inc.signal_type.value}/{inc.severity.value} "
                f"peak={inc.peak_score:.2f}{extra}",
                file=self._out,
            )
        elif isinstance(output, StateChange):
            print(f"[{at}] state {d['from']} → {d['to']} ({d['reason']})", file=self._out)
        else:
            print(
                f"[{at}] shadow_detection {d['signal_type']} (experimental, no alert) "
                f"onset≈{fmt_time(d['started_at'])} peak={d['peak_score']:.2f}",
                file=self._out,
            )


def build_summary(result: PipelineResult, source_info: dict[str, Any]) -> dict[str, Any]:
    stats = result.stats
    cfg = result.config
    return {
        "vigil_edge_version": __version__,
        "rule_version": cfg.rule_version,
        "disclaimer": "Experimental motion analysis. Scores are uncalibrated; detections are "
        "possible events for human review, not diagnoses.",
        "source": source_info,
        "frames_processed": stats.frames,
        "windows": stats.windows,
        "media_seconds": round(stats.media_seconds, 3),
        "processing": {
            "wall_seconds": round(stats.wall_seconds, 3),
            "analysis_fps": round(stats.processing_fps, 1),
            "realtime_factor": round(stats.realtime_factor, 2),
            "stage_ms_per_frame": {
                k: round(1000 * v / max(stats.frames, 1), 3) for k, v in stats.stage_seconds.items()
            },
        },
        "incidents": [
            {
                "id": i.id,
                "signal_type": i.signal_type.value,
                "severity": i.severity.value,
                "onset_estimate": round(i.started_at, 2),
                "opened_at": round(i.opened_at, 2),
                "ended_at": None if i.ended_at is None else round(i.ended_at, 2),
                "detection_delay_from_onset_estimate": round(i.opened_at - i.started_at, 2),
                "peak_score": round(i.peak_score, 3),
                "follows_incident_id": i.follows_incident_id,
            }
            for i in result.incidents
        ],
        "shadow_detections": [output_to_dict(s) for s in result.shadow_detections],
        "config": config_to_dict(cfg),
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
