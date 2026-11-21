"""Command-line interface: ``vigil analyze | evaluate | synth | config``.

Examples::

    vigil analyze clip.mp4 --plot --overlay
    vigil analyze synthetic:convulsion --plot
    vigil analyze phone.mov --config config/handheld.toml --set detectors.convulsion.threshold=0.6
    vigil evaluate --synthetic
    vigil evaluate data/labeled/            # every video with a <name>.labels.json
    vigil synth mixed -o data/synthetic/mixed.mp4
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vigil_edge import __version__
from vigil_edge.config import ConfigError, VigilConfig, config_from_dict, config_to_dict
from vigil_edge.contracts import incident_event_message
from vigil_edge.evaluation import (
    SignalMetrics,
    detections_from_result,
    evaluate,
    labels_to_json,
    load_labels,
)
from vigil_edge.models import IncidentEvent, Label, SignalType
from vigil_edge.outputs import (
    ConsoleReporter,
    EventsJsonlWriter,
    FrameCsvWriter,
    TimelineCsvWriter,
    build_summary,
    fmt_time,
    write_summary,
)
from vigil_edge.pipeline import PipelineObserver, PipelineResult, run_pipeline
from vigil_edge.sources.base import FrameSource, SourceError
from vigil_edge.sources.synthetic import PRESETS, SyntheticSource, get_scenario
from vigil_edge.sources.video_file import VideoFileSource

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}
DISCLAIMER = (
    "Experimental motion analysis — detections are possible events for review, not diagnoses."
)


class CliError(Exception):
    pass


# ----------------------------------------------------------------- config


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _parse_set(expr: str) -> dict[str, Any]:
    if "=" not in expr:
        raise CliError(f"--set expects key=value, got {expr!r}")
    key, raw = expr.split("=", 1)
    try:
        value = tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        value = raw  # bare strings: --set subject.name=Biscuit
    node: dict[str, Any] = {}
    cur = node
    parts = key.strip().split(".")
    for p in parts[:-1]:
        cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value
    return node


def build_config(
    paths: Sequence[str], sets: Sequence[str], extra: dict[str, Any] | None = None
) -> VigilConfig:
    data: dict[str, Any] = {}
    for p in paths:
        try:
            with open(p, "rb") as fh:
                data = _deep_merge(data, tomllib.load(fh))
        except FileNotFoundError:
            raise CliError(f"config file not found: {p}") from None
        except tomllib.TOMLDecodeError as exc:
            raise CliError(f"invalid TOML in {p}: {exc}") from None
    if extra:
        data = _deep_merge(data, extra)
    for s in sets:
        data = _deep_merge(data, _parse_set(s))
    return config_from_dict(data)


# ----------------------------------------------------------------- sources


@dataclass
class OpenedInput:
    source: FrameSource
    name: str
    info: dict[str, Any]
    labels: list[Label] | None


def open_input(
    spec: str,
    cfg: VigilConfig,
    *,
    start: float,
    end: float | None,
    seed: int,
    labels_path: str | None,
) -> OpenedInput:
    labels: list[Label] | None = None
    if spec.startswith("synthetic:"):
        name = spec.split(":", 1)[1]
        try:
            scenario = get_scenario(name)
        except KeyError as exc:
            raise CliError(str(exc.args[0])) from None
        src: FrameSource = SyntheticSource(scenario, fps=cfg.source.target_fps, seed=seed)
        labels = scenario.labels
        info = {
            "kind": "synthetic",
            "scenario": name,
            "seed": seed,
            "description": scenario.description,
            "size": list(src.frame_size),
            "fps": src.fps,
        }
        display = f"synthetic-{name}"
    else:
        path = Path(spec)
        vsrc = VideoFileSource(path, target_fps=cfg.source.target_fps, start=start, end=end)
        src = vsrc
        info = {
            "kind": "file",
            "name": path.name,
            "size": list(vsrc.frame_size),
            "native_fps": round(vsrc.native_fps, 3),
            "analysis_fps": vsrc.fps,
            "duration_s": round(vsrc.duration, 2),
            "start": start,
            "end": end,
        }
        display = path.stem
        sibling = path.with_name(path.stem + ".labels.json")
        if labels_path is None and sibling.is_file():
            labels_path = str(sibling)
    if labels_path is not None:
        labels = load_labels(Path(labels_path))
        info["labels"] = labels_path
    return OpenedInput(src, display, info, labels)


# ----------------------------------------------------------------- printing


def _print_result(result: PipelineResult, inp: OpenedInput, out_dir: Path | None) -> None:
    st = result.stats
    print(f"\nVigil offline analysis — {DISCLAIMER}")
    size = "x".join(str(v) for v in inp.info.get("size", []))
    print(f"input     {inp.name} ({size})")
    print(f"media     {fmt_time(st.media_seconds)} · {st.frames} frames · {st.windows} windows")
    print(
        f"speed     {st.processing_fps:.0f} analysis fps ({st.realtime_factor:.0f}x realtime "
        f"on this machine)"
    )
    print(f"rules     {result.config.rule_version}")
    print(f"\nIncidents ({len(result.incidents)}):")
    if not result.incidents:
        print("  none")
    for n, inc in enumerate(result.incidents, 1):
        follows = "  follows earlier incident" if inc.follows_incident_id else ""
        print(
            f"  #{n} {inc.signal_type.value}/{inc.severity.value:<7} onset≈"
            f"{fmt_time(inc.started_at)}  opened {fmt_time(inc.opened_at)} "
            f"(+{inc.opened_at - inc.started_at:.1f}s)  ended {fmt_time(inc.ended_at)}  "
            f"peak {inc.peak_score:.2f}{follows}"
        )
    shadows = result.shadow_detections
    print(f"\nShadow detections — experimental, never alert ({len(shadows)}):")
    if not shadows:
        print("  none")
    for sd in shadows:
        print(
            f"  {sd.signal_type.value:<10} onset≈{fmt_time(sd.started_at)}  confirmed "
            f"{fmt_time(sd.at)}  peak {sd.peak_score:.2f}"
        )
    if inp.labels is not None:
        metrics = evaluate(detections_from_result(result), inp.labels)
        hours = st.media_seconds / 3600
        print("\nAgainst labels (this clip only — not a validated accuracy figure):")
        _print_metrics(metrics.values(), hours)
    if out_dir is not None:
        print(f"\nOutputs   {out_dir}/")


def _print_metrics(metrics: Any, hours: float) -> None:
    for m in metrics:
        d = m.as_dict(hours)
        if d["labeled_events"] == 0 and d["false_alerts"] == 0:
            continue
        lat = d["latency_s"]
        lat_s = (
            f"latency median {lat['median']}s (min {lat['min']}, max {lat['max']})"
            if lat
            else "latency n/a"
        )
        recall = "n/a" if d["recall"] is None else f"{d['detected_events']}/{d['labeled_events']}"
        print(
            f"  {d['signal_type']:<10} detected {recall:<6} false alerts "
            f"{d['false_alerts']}  {lat_s}"
        )


# ----------------------------------------------------------------- commands


def _common_config_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "-c",
        "--config",
        action="append",
        default=[],
        metavar="TOML",
        help="config file(s); later files override earlier ones",
    )
    p.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override one setting, e.g. detectors.convulsion.threshold=0.6",
    )


def cmd_analyze(args: argparse.Namespace) -> int:
    extra: dict[str, Any] = {}
    if args.fps:
        extra["source"] = {"target_fps": args.fps}
    if args.stabilize:
        extra["motion"] = {"stabilize": True}
    cfg = build_config(args.config, args.set, extra)
    inp = open_input(
        args.input, cfg, start=args.start, end=args.end, seed=args.seed, labels_path=args.labels
    )

    out_dir: Path | None = None
    observers: list[PipelineObserver] = []
    if not args.no_output:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = Path(args.out) if args.out else Path("runs") / f"{inp.name}-{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)
        observers += [
            EventsJsonlWriter(out_dir / "events.jsonl"),
            TimelineCsvWriter(out_dir / "windows.csv", ["convulsion", "vomiting"]),
        ]
        if args.frame_csv:
            observers.append(FrameCsvWriter(out_dir / "frames.csv"))
        if args.overlay:
            from vigil_edge.overlay import OverlayVideoWriter

            observers.append(OverlayVideoWriter(out_dir / "overlay.mp4", inp.source.fps))
    if not args.quiet:
        observers.append(ConsoleReporter())

    result = run_pipeline(inp.source, cfg, observers=observers)

    if out_dir is not None:
        summary = build_summary(result, inp.info)
        if inp.labels is not None:
            metrics = evaluate(detections_from_result(result), inp.labels)
            summary["evaluation"] = [
                m.as_dict(result.stats.media_seconds / 3600) for m in metrics.values()
            ]
            (out_dir / "labels.json").write_text(json.dumps(labels_to_json(inp.labels), indent=2))
        write_summary(out_dir / "summary.json", summary)
        if args.recorded_at:
            started = datetime.fromisoformat(args.recorded_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            with (out_dir / "incident_messages.jsonl").open("w") as fh:
                for ev in result.outputs:
                    if isinstance(ev, IncidentEvent):
                        msg = incident_event_message(ev, args.device_id, started)
                        fh.write(json.dumps(msg) + "\n")
        if args.plot:
            from vigil_edge.plotting import plot_timeline

            plot_timeline(
                result, out_dir / "timeline.png", inp.labels, title=f"Vigil timeline — {inp.name}"
            )
    _print_result(result, inp, out_dir)
    return 0


def _collect_eval_items(paths: Sequence[str]) -> list[tuple[Path, Path]]:
    items: list[tuple[Path, Path]] = []
    for raw in paths:
        p = Path(raw)
        candidates = (
            sorted(q for q in p.iterdir() if q.suffix.lower() in VIDEO_EXTS) if p.is_dir() else [p]
        )
        for video in candidates:
            labels = video.with_name(video.stem + ".labels.json")
            if not labels.is_file():
                if p.is_dir():
                    print(f"skip {video.name}: no {labels.name}", file=sys.stderr)
                    continue
                raise CliError(f"{video}: missing labels file {labels.name}")
            items.append((video, labels))
    return items


def cmd_evaluate(args: argparse.Namespace) -> int:
    cfg = build_config(args.config, args.set)
    runs: list[tuple[str, PipelineResult, list[Label]]] = []
    if args.synthetic:
        # Default set: fixed-camera scenarios. Handheld ones need config/handheld.toml.
        names = args.paths or [n for n, f in PRESETS.items() if f().fixed_camera]
        for name in names:
            scenario = get_scenario(name)
            res = run_pipeline(
                SyntheticSource(scenario, fps=cfg.source.target_fps, seed=args.seed), cfg
            )
            runs.append((f"synthetic:{name}", res, scenario.labels))
    else:
        items = _collect_eval_items(args.paths)
        if not items:
            raise CliError("nothing to evaluate (need videos with <name>.labels.json)")
        for video, labels_file in items:
            res = run_pipeline(VideoFileSource(video, target_fps=cfg.source.target_fps), cfg)
            runs.append((video.name, res, load_labels(labels_file)))

    totals = {s: SignalMetrics(s) for s in (SignalType.CONVULSION, SignalType.VOMITING)}
    hours = 0.0
    per_clip = []
    print(f"Vigil evaluation — {DISCLAIMER}\nrules {cfg.rule_version}\n")
    print(f"{'clip':<24} {'media':>7}  {'labels':>6}  {'incidents':>9}  {'shadow':>6}  result")
    for name, res, labels in runs:
        metrics = evaluate(detections_from_result(res), labels, tolerance=args.tolerance)
        clip_hours = res.stats.media_seconds / 3600
        hours += clip_hours
        for s, m in metrics.items():
            totals[s].merge(m)
        missed = sum(m.labeled_events - m.detected_events for m in metrics.values())
        false = sum(m.false_alerts for m in metrics.values())
        verdict = "ok" if not missed and not false else f"missed {missed}, false {false}"
        print(
            f"{name:<24} {fmt_time(res.stats.media_seconds):>7}  {len(labels):>6}  "
            f"{len(res.incidents):>9}  {len(res.shadow_detections):>6}  {verdict}"
        )
        per_clip.append(
            {
                "clip": name,
                "media_seconds": round(res.stats.media_seconds, 2),
                "metrics": [m.as_dict(clip_hours) for m in metrics.values()],
            }
        )
    print(
        f"\nTotals over {len(runs)} clips, {hours * 60:.1f} min of footage "
        "(dataset-specific; not a real-world accuracy estimate):"
    )
    _print_metrics(totals.values(), hours)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(
                {
                    "rule_version": cfg.rule_version,
                    "clips": per_clip,
                    "totals": [m.as_dict(hours) for m in totals.values()],
                    "footage_hours": round(hours, 4),
                },
                indent=2,
            )
        )
    return 0


def cmd_synth(args: argparse.Namespace) -> int:
    if args.list or not args.scenario:
        for name, factory in PRESETS.items():
            print(f"{name:<11} {factory().description}")
        return 0
    try:
        scenario = get_scenario(args.scenario)
    except KeyError as exc:
        raise CliError(str(exc.args[0])) from None
    out = Path(args.output or f"data/synthetic/{scenario.name}.mp4")
    out.parent.mkdir(parents=True, exist_ok=True)
    import cv2

    src = SyntheticSource(
        scenario, fps=args.fps, seed=args.seed, size=(args.width, round(args.width * 9 / 16))
    )
    writer = cv2.VideoWriter(str(out), cv2.VideoWriter.fourcc(*"mp4v"), args.fps, src.frame_size)
    if not writer.isOpened():
        raise CliError(f"could not write {out}")
    for frame in src.frames():
        writer.write(frame.image)
    writer.release()
    labels_path = out.with_name(out.stem + ".labels.json")
    labels_path.write_text(json.dumps(labels_to_json(scenario.labels), indent=2) + "\n")
    print(f"wrote {out} ({scenario.duration:.0f}s @ {args.fps} fps) and {labels_path.name}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = build_config(args.config, args.set)
    print(json.dumps({"rule_version": cfg.rule_version, **config_to_dict(cfg)}, indent=2))
    return 0


# ----------------------------------------------------------------- service


def service_config(args: argparse.Namespace) -> VigilConfig:
    """Config files (``-c`` or $VIGIL_CONFIG) + --set, then env/flag service overrides."""
    paths = list(args.config) or (
        [os.environ["VIGIL_CONFIG"]] if os.environ.get("VIGIL_CONFIG") else []
    )
    cfg = build_config(paths, args.set)
    svc = cfg.service
    device_id = getattr(args, "device_id", None) or os.environ.get("VIGIL_DEVICE_ID")
    data_dir = getattr(args, "data_dir", None) or os.environ.get("VIGIL_DATA_DIR")
    if device_id or data_dir:
        svc = replace(svc, device_id=device_id or svc.device_id, data_dir=data_dir or svc.data_dir)
        cfg = replace(cfg, service=svc)
    return cfg


def make_source_factory(
    spec: str, cfg: VigilConfig, realtime: bool, loop: bool, speed: float
) -> Any:
    from vigil_edge.sources.live import LoopingSource, OpenCVCameraSource, PacedSource

    if spec == "camera":
        if cfg.camera.backend == "picamera2":
            from vigil_edge.sources.picamera2_source import Picamera2Source

            return lambda: Picamera2Source(
                cfg.camera, cfg.recording.pre_event_seconds, record=cfg.recording.enabled
            )
        return lambda: OpenCVCameraSource(cfg.camera.device, cfg.camera.fps)
    if spec.startswith("opencv:"):
        device = int(spec.split(":", 1)[1])
        return lambda: OpenCVCameraSource(device, cfg.camera.fps)
    if spec.startswith("synthetic:"):
        scenario = get_scenario(spec.split(":", 1)[1])

        def base() -> FrameSource:
            return SyntheticSource(scenario, fps=cfg.source.target_fps)
    elif spec.startswith("file:"):
        path = Path(spec.split(":", 1)[1])
        VideoFileSource(path).close()  # fail fast on a bad path

        def base() -> FrameSource:
            return VideoFileSource(path, target_fps=cfg.source.target_fps)
    else:
        raise CliError(f"unknown source {spec!r} (camera | opencv:N | file:PATH | synthetic:NAME)")

    def factory() -> FrameSource:
        src: FrameSource = LoopingSource(base) if loop else base()
        return PacedSource(src, speed=speed) if realtime else src

    return factory


def _setup_logging() -> None:
    level = os.environ.get("VIGIL_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def cmd_run(args: argparse.Namespace) -> int:
    from vigil_edge.service import EdgeService

    _setup_logging()
    cfg = service_config(args)
    factory = make_source_factory(
        args.source, cfg, realtime=not args.fast, loop=args.loop, speed=args.speed
    )
    service = EdgeService(cfg, factory, cloud_token=os.environ.get("VIGIL_DEVICE_TOKEN"))

    def stop(signum: int, _frame: object) -> None:
        logging.getLogger("vigil").info("signal %d: shutting down", signum)
        service.stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    service.run()
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from vigil_edge import bench

    _setup_logging()
    cfg = build_config(args.config, args.set)
    if args.camera:
        report = bench.bench_camera(cfg, seconds=args.seconds)
    else:
        report = bench.bench_offline(cfg, seconds=args.seconds)
    text = json.dumps(report, indent=2)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(text + "\n")
    if report["kind"] == "offline":
        h = report["host"]
        print(
            f"Offline analysis benchmark on {h['model'] or h['cpu'] or h['machine']} "
            f"({report['input']['analysis_size'][0]}x{report['input']['analysis_size'][1]} "
            f"@ {report['input']['fps']} fps target)"
        )
        print(f"{'variant':<26} {'ms/frame p50':>12} {'p95':>7} {'max fps':>8} {'CPU @target':>11}")
        for r in report["results"]:
            ms = r["ms_per_frame"]
            print(
                f"{r['variant']:<26} {ms['p50']:>12} {ms['p95']:>7} "
                f"{r['max_analysis_fps']:>8} {r['cpu_percent_at_target_fps']:>10}%"
            )
    else:
        print(text)
    return 0


def cmd_maintenance(args: argparse.Namespace) -> int:
    from vigil_edge.service import ServicePaths

    paths = ServicePaths(Path(service_config(args).service.data_dir))
    flag = paths.maintenance_flag
    if args.action == "on":
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(datetime.now(UTC).isoformat())
    elif args.action == "off":
        flag.unlink(missing_ok=True)
    print(f"maintenance {'ON' if flag.exists() else 'off'} ({flag})")
    return 0


def cmd_test_alert(args: argparse.Namespace) -> int:
    """Queue a TEST text for the running service to deliver (uses its device token)."""
    import time
    import uuid

    from vigil_edge.contracts import build_test_alert
    from vigil_edge.service import ServicePaths
    from vigil_edge.store import LocalStore

    cfg = service_config(args)
    if not cfg.cloud.enabled:
        print(
            "cloud sync is not enabled in this config; nothing would be sent "
            "(on the Pi pass -c /etc/vigil/vigil.toml)"
        )
        return 1
    paths = ServicePaths(Path(cfg.service.data_dir))
    if not paths.db.exists():
        print(f"no Vigil data at {paths.data_dir}; is the service set up? (--data-dir)")
        return 1
    # The device id must match the one the service authenticates as. `sudo -u vigil`
    # does not load /etc/vigil/vigil.env, so prefer the running service's heartbeat.
    device_id = getattr(args, "device_id", None)
    if not device_id and paths.heartbeat_file.exists():
        with contextlib.suppress(ValueError, KeyError, OSError):
            device_id = json.loads(paths.heartbeat_file.read_text())["device_id"]
    device_id = device_id or cfg.service.device_id
    request_id = str(uuid.uuid4())
    store = LocalStore(paths.db)
    try:
        msg = build_test_alert(device_id, args.to, request_id, datetime.now(UTC))
        store.enqueue("test_alert", msg, datetime.now(UTC), f"test_alert:{request_id}")
        who = "every household phone" if args.to == "all" else "administrators"
        print(f"test alert queued for {who}; waiting for the running service to deliver it...")
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            status = store.message_status(f"test_alert:{request_id}")
            if status and status[0]:
                print(
                    "delivered to the cloud. Texts go out within seconds "
                    "(at most 3 test alerts per hour; extra ones are ignored)."
                )
                return 0
            if status and status[1] > 0:
                print("the cloud rejected the request; see `journalctl -u vigil-edge`")
                return 1
            time.sleep(1)
    finally:
        store.close()
    print(
        f"not delivered within {args.wait:.0f}s. It stays queued and goes out once the "
        "service is running and online (check `systemctl status vigil-edge`)."
    )
    return 2


def cmd_clips(args: argparse.Namespace) -> int:
    from vigil_edge.service import ServicePaths
    from vigil_edge.store import LocalStore

    paths = ServicePaths(Path(service_config(args).service.data_dir))
    store = LocalStore(paths.db)
    try:
        if args.action == "list":
            clips = store.list_clips()
            for c in clips:
                keep = "PRESERVED" if c.preserved else f"expires {c.expires_at:%Y-%m-%d}"
                print(
                    f"{c.id}  {c.kind:<8} {c.started_at:%Y-%m-%d %H:%M:%S}Z  "
                    f"{(c.ended_at - c.started_at).total_seconds():5.0f}s  "
                    f"{c.bytes / 1e6:6.1f} MB  {keep}"
                )
            print(f"{len(clips)} clips, {store.clips_bytes() / 1e9:.2f} GB")
            return 0
        if not args.clip_id:
            raise CliError(f"clips {args.action} needs a clip id")
        ok = store.set_preserved(args.clip_id, args.action == "preserve", datetime.now(UTC))
        if not ok:
            raise CliError(f"no clip {args.clip_id}")
        print(f"clip {args.clip_id} {args.action}d")
        return 0
    finally:
        store.close()


def cmd_health(args: argparse.Namespace) -> int:
    from vigil_edge.service import ServicePaths
    from vigil_edge.store import parse_iso

    paths = ServicePaths(Path(service_config(args).service.data_dir))
    if not paths.heartbeat_file.is_file():
        print("no heartbeat yet — is `vigil run` running?")
        return 1
    hb = json.loads(paths.heartbeat_file.read_text())
    age = (datetime.now(UTC) - parse_iso(hb["sent_at"])).total_seconds()
    print(json.dumps(hb, indent=2))
    print(f"\nlast heartbeat {age:.0f}s ago")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vigil", description="Vigil edge tools. " + DISCLAIMER)
    parser.add_argument("--version", action="version", version=f"vigil-edge {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("analyze", help="analyze a video file or synthetic scenario")
    a.add_argument("input", help="video path, or synthetic:<scenario>")
    _common_config_args(a)
    a.add_argument("-o", "--out", help="output directory (default runs/<name>-<timestamp>)")
    a.add_argument("--fps", type=float, help="analysis frame rate (overrides config)")
    a.add_argument("--start", type=float, default=0.0, help="start offset in seconds")
    a.add_argument("--end", type=float, help="end offset in seconds")
    a.add_argument("--stabilize", action="store_true", help="compensate camera motion")
    a.add_argument("--labels", help="labels JSON (default: <video>.labels.json if present)")
    a.add_argument("--plot", action="store_true", help="write timeline.png (needs matplotlib)")
    a.add_argument("--overlay", action="store_true", help="write overlay.mp4 debug video")
    a.add_argument("--frame-csv", action="store_true", help="write per-frame features")
    a.add_argument("--no-output", action="store_true", help="print only; write no files")
    a.add_argument("--quiet", "-q", action="store_true", help="no live event log")
    a.add_argument("--seed", type=int, default=0, help="seed for synthetic inputs")
    a.add_argument("--recorded-at", help="ISO time the video started; writes contract messages")
    a.add_argument("--device-id", default="offline-analysis", help="device id for messages")
    a.set_defaults(func=cmd_analyze)

    e = sub.add_parser("evaluate", help="event-level metrics on labeled footage")
    e.add_argument(
        "paths", nargs="*", help="videos/directories (or scenario names with --synthetic)"
    )
    e.add_argument("--synthetic", action="store_true", help="evaluate synthetic scenarios")
    _common_config_args(e)
    e.add_argument(
        "--tolerance",
        type=float,
        default=10.0,
        help="seconds after a label's end that a detection may still match",
    )
    e.add_argument("--seed", type=int, default=0)
    e.add_argument("--json-out", help="write the full report as JSON")
    e.set_defaults(func=cmd_evaluate)

    s = sub.add_parser("synth", help="render a synthetic scenario to a video + labels")
    s.add_argument("scenario", nargs="?", help="scenario name (see --list)")
    s.add_argument("-o", "--output", help="output .mp4 (default data/synthetic/<name>.mp4)")
    s.add_argument("--list", action="store_true", help="list scenarios")
    s.add_argument("--fps", type=float, default=30.0)
    s.add_argument("--width", type=int, default=640)
    s.add_argument("--seed", type=int, default=0)
    s.set_defaults(func=cmd_synth)

    c = sub.add_parser("config", help="print the effective configuration")
    _common_config_args(c)
    c.set_defaults(func=cmd_config)

    def service_args(p: argparse.ArgumentParser) -> None:
        _common_config_args(p)
        p.add_argument("--data-dir", help="data directory (default $VIGIL_DATA_DIR or config)")

    r = sub.add_parser("run", help="run the monitoring service (live camera or simulation)")
    service_args(r)
    r.add_argument(
        "--source",
        default="camera",
        help="camera | opencv:N | file:PATH | synthetic:NAME (default camera)",
    )
    r.add_argument("--device-id", help="device id (default $VIGIL_DEVICE_ID or config)")
    r.add_argument("--loop", action="store_true", help="replay file/synthetic sources forever")
    r.add_argument(
        "--fast",
        action="store_true",
        help="file/synthetic: process as fast as possible instead of real time",
    )
    r.add_argument("--speed", type=float, default=1.0, help="real-time multiplier for replay")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("bench", help="benchmark analysis (offline) or live camera (Pi)")
    _common_config_args(b)
    b.add_argument("--camera", action="store_true", help="live Picamera2 benchmark (Pi only)")
    b.add_argument("--seconds", type=float, default=60.0)
    b.add_argument("--json-out", help="write the report as JSON")
    b.set_defaults(func=cmd_bench)

    m = sub.add_parser("maintenance", help="maintenance mode: no alerts or clips")
    service_args(m)
    m.add_argument("action", choices=["on", "off", "status"])
    m.set_defaults(func=cmd_maintenance)

    cl = sub.add_parser("clips", help="list clips; preserve/unpreserve (admin, on device)")
    service_args(cl)
    cl.add_argument("action", choices=["list", "preserve", "unpreserve"])
    cl.add_argument("clip_id", nargs="?")
    cl.set_defaults(func=cmd_clips)

    ta = sub.add_parser("test-alert", help="send a TEST text to the household (no incident)")
    service_args(ta)
    ta.add_argument(
        "--to", choices=["all", "admin"], default="all", help="all phones (default) or admins"
    )
    ta.add_argument("--wait", type=float, default=30.0, help="seconds to wait for delivery")
    ta.add_argument("--device-id", help="override (default: the running service's device id)")
    ta.set_defaults(func=cmd_test_alert)

    h = sub.add_parser("health", help="show the latest local heartbeat")
    service_args(h)
    h.set_defaults(func=cmd_health)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code: int = args.func(args)
        return code
    except (CliError, ConfigError, SourceError, ValueError) as exc:
        print(f"vigil: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
