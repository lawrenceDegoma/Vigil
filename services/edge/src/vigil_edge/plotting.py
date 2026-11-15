"""Timeline plot for offline analysis (optional ``plot`` extra, Mac only).

Small multiples sharing the time axis, one y-scale per panel:
motion activity, rhythm, and detector scores. Incidents are shaded, labeled
ground truth is hatched, shadow detections are dashed verticals.
"""

from __future__ import annotations

from pathlib import Path

from vigil_edge.models import DetectorMode, Label, Severity
from vigil_edge.pipeline import PipelineResult

# Reference palette (dataviz skill, light mode).
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_2 = "#52514e"
GRID = "#e4e3df"
SERIES = ("#2a78d6", "#eb6834")
STATUS = {Severity.URGENT: "#e34948", Severity.WARNING: "#eda100"}


def plot_timeline(
    result: PipelineResult, path: Path, labels: list[Label] | None = None, title: str = ""
) -> Path:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "plotting needs matplotlib: run `make setup` or `uv sync --extra plot`"
        ) from exc

    w = result.windows
    if not w:
        raise ValueError("no analysis windows to plot (input shorter than one window?)")
    t = [r.features.t_end for r in w]

    def series(attr: str) -> list[float]:
        return [float(getattr(r.features, attr)) for r in w]

    def score(det: str) -> list[float]:
        """Effective score: suppressed windows count as 0 (they can never be active)."""
        out = []
        for r in w:
            res = r.result(det)
            out.append(0.0 if res is None or res.suppressed_by else res.score)
        return out

    plt.rcParams.update(
        {
            "font.size": 9,
            "axes.edgecolor": GRID,
            "axes.labelcolor": TEXT_2,
            "xtick.color": TEXT_2,
            "ytick.color": TEXT_2,
            "text.color": TEXT,
            "axes.spines.top": False,
            "axes.spines.right": False,
        }
    )
    fig, axes = plt.subplots(3, 1, figsize=(12, 7.5), sharex=True, facecolor=SURFACE)
    panels = [
        (
            "Motion (fraction of ROI)",
            [("activity", series("activity")), ("slow activity", series("slow_activity"))],
        ),
        (
            "Rhythm (spectral concentration)",
            [("convulsion band", series("periodicity")), ("slow band", series("low_periodicity"))],
        ),
        (
            "Detector score (uncalibrated)",
            [
                (
                    r.detector + (" (shadow)" if r.mode is DetectorMode.SHADOW else ""),
                    score(r.detector),
                )
                for r in w[0].results
            ],
        ),
    ]
    for ax, (ylabel, lines) in zip(axes, panels, strict=True):
        ax.set_facecolor(SURFACE)
        ax.grid(axis="y", color=GRID, linewidth=0.6)
        for lb in labels or []:
            ax.axvspan(
                lb.start,
                lb.end,
                facecolor="none",
                edgecolor="#9a9893",
                hatch="//",
                linewidth=0,
                zorder=0,
            )
        for inc in result.incidents:
            end = inc.ended_at if inc.ended_at is not None else t[-1]
            ax.axvspan(
                inc.started_at, end, color=STATUS[inc.severity], alpha=0.12, linewidth=0, zorder=0
            )
        for sd in result.shadow_detections:
            ax.axvline(sd.at, color=TEXT_2, linestyle="--", linewidth=1)
        for (name, ys), colour in zip(lines, SERIES, strict=False):
            ax.plot(t, ys, color=colour, linewidth=2, label=name)
        ax.set_ylabel(ylabel)
        ax.legend(
            loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, ncol=3, borderaxespad=0.1
        )

    score_ax = axes[2]
    score_ax.set_ylim(-0.08, 1.05)
    primary = w[0].results[0].detector  # the first detector is the profile's primary alert
    suppressed = [r.features.t_end for r in w if (res := r.result(primary)) and res.suppressed_by]
    if suppressed:
        score_ax.plot(
            suppressed,
            [-0.04] * len(suppressed),
            "|",
            color=TEXT_2,
            markersize=6,
            label=f"{primary} suppressed",
        )
        score_ax.legend(
            loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, ncol=3, borderaxespad=0.1
        )
    cfg = result.config.detectors
    for thr, colour in ((cfg.convulsion.threshold, SERIES[0]), (cfg.vomiting.threshold, SERIES[1])):
        score_ax.axhline(thr, color=colour, linestyle=":", linewidth=1)
    for inc in result.incidents:
        score_ax.annotate(
            f"{inc.signal_type.value} opened",
            xy=(inc.opened_at, 1.0),
            xytext=(3, 0),
            textcoords="offset points",
            fontsize=8,
            color=TEXT_2,
            va="bottom",
        )
        score_ax.axvline(inc.opened_at, color=STATUS[inc.severity], linewidth=1)
    score_ax.set_xlabel("time (s)")
    caption = "hatched = labeled ground truth · shaded = incident · dashed = shadow detection"
    fig.suptitle(title or "Vigil timeline", x=0.01, ha="left", fontsize=11, color=TEXT)
    fig.text(
        0.01,
        0.005,
        f"{caption} · {result.config.rule_version} · experimental, not a diagnosis",
        fontsize=7.5,
        color=TEXT_2,
    )
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(path, dpi=130, facecolor=SURFACE)
    plt.close(fig)
    return path
