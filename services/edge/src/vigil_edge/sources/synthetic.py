"""Procedural synthetic scenarios with ground-truth labels.

These exist to exercise the pipeline deterministically without hardware or
private footage: a textured "subject" on a living-room-like background,
performing scripted behaviours (resting, walking, scratching, convulsion-like
shaking, retching-like heaving) plus disturbances (lighting changes, camera
shake, a person walking through).

Synthetic results validate plumbing and gross discrimination only. They say
nothing about real-world detection accuracy.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from vigil_edge.models import Frame, Image, Label, SignalType
from vigil_edge.sources.base import FrameSource

Point = tuple[float, float]

BEHAVIOURS = ("rest", "walk", "convulsion", "scratch", "shake_off", "retch", "absent")
LABEL_MERGE_GAP = 10.0
EFFECTS = ("lighting_step", "lighting_ramp", "camera_shake", "camera_bump", "person")


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    behaviour: str
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.behaviour not in BEHAVIOURS:
            raise ValueError(f"unknown behaviour {self.behaviour!r}")
        if self.end <= self.start:
            raise ValueError("segment end must be after start")


@dataclass(frozen=True)
class Effect:
    start: float
    end: float
    kind: str
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in EFFECTS:
            raise ValueError(f"unknown effect {self.kind!r}")


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    duration: float
    segments: tuple[Segment, ...]
    effects: tuple[Effect, ...] = ()

    fixed_camera: bool = True  # False → evaluate with config/handheld.toml

    @property
    def labels(self) -> list[Label]:
        """Ground truth; same-type segments ≤ ``LABEL_MERGE_GAP`` apart are one episode."""
        mapping = {"convulsion": SignalType.CONVULSION, "retch": SignalType.VOMITING}
        out: list[Label] = []
        for s in self.segments:
            if s.behaviour not in mapping:
                continue
            sig = mapping[s.behaviour]
            if out and out[-1].signal_type is sig and s.start - out[-1].end <= LABEL_MERGE_GAP:
                out[-1] = Label(sig, out[-1].start, s.end, out[-1].note)
            else:
                out.append(Label(sig, s.start, s.end, f"synthetic {s.behaviour}"))
        return out


@dataclass
class _Pose:
    x: float
    y: float
    angle: float = 0.0  # degrees
    scale: float = 1.0
    legs: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # swing, radians
    head_dy: float = 0.0  # fraction of width
    facing: int = 1  # +1 right, -1 left
    visible: bool = True


def _sin(hz: float, t: float, phase: float = 0.0) -> float:
    return math.sin(2.0 * math.pi * hz * t + phase)


class SyntheticSource(FrameSource):
    def __init__(
        self,
        scenario: Scenario,
        fps: float = 15.0,
        size: tuple[int, int] = (640, 360),
        seed: int = 0,
        noise_sigma: float = 2.5,
    ) -> None:
        self.scenario = scenario
        self._fps = fps
        self._size = size
        self._seed = seed
        rng = np.random.default_rng(seed)
        self._background = self._make_background(rng)
        # Precomputed sensor-noise bank (drawing fresh Gaussian noise per frame
        # dominates render time); frames pick a random entry.
        h, w = size[1], size[0]
        self._noise_bank = (
            [rng.normal(0.0, noise_sigma, (h, w, 3)).astype(np.int16) for _ in range(24)]
            if noise_sigma > 0
            else []
        )

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._size

    # ------------------------------------------------------------ rendering

    def _make_background(self, rng: np.random.Generator) -> Image:
        w, h = self._size
        coarse = rng.integers(80, 150, size=(h // 24 + 2, w // 24 + 2)).astype(np.float32)
        base = cv2.resize(coarse, (w, h), interpolation=cv2.INTER_CUBIC)
        fine = rng.normal(0, 6, size=(h, w)).astype(np.float32)
        base = cv2.GaussianBlur(base + fine, (3, 3), 0)
        img = np.clip(np.stack([base * 0.9, base, base * 1.1], axis=2), 0, 255).astype(np.uint8)
        floor_y = int(h * 0.45)
        img[floor_y:] = (img[floor_y:].astype(np.int16) - 25).clip(0, 255).astype(np.uint8)
        cv2.rectangle(
            img, (int(w * 0.02), int(h * 0.18)), (int(w * 0.32), int(h * 0.5)), (70, 60, 120), -1
        )  # couch
        cv2.rectangle(
            img, (int(w * 0.75), int(h * 0.1)), (int(w * 0.9), int(h * 0.42)), (40, 80, 90), -1
        )  # shelf
        cv2.ellipse(
            img,
            (int(w * 0.55), int(h * 0.75)),
            (int(w * 0.3), int(h * 0.15)),
            0,
            0,
            360,
            (110, 130, 150),
            -1,
        )  # rug
        result: Image = img
        return result

    def _pose(self, t: float, rng: np.random.Generator) -> _Pose:
        seg = next((s for s in self.scenario.segments if s.start <= t < s.end), None)
        if seg is None:
            return _Pose(0.5, 0.65)
        p = seg.params
        at: Point = tuple(p.get("at", (0.5, 0.65)))
        lt = t - seg.start
        b = seg.behaviour
        if b == "absent":
            return _Pose(*at, visible=False)
        if b == "rest":
            return _Pose(*at, scale=1.0 + 0.01 * _sin(0.3, lt), facing=p.get("facing", 1))
        if b == "walk":
            src: Point = tuple(p.get("from", (0.2, 0.65)))
            dst: Point = tuple(p.get("to", (0.8, 0.65)))
            k = lt / (seg.end - seg.start)
            x = src[0] + (dst[0] - src[0]) * k
            y = src[1] + (dst[1] - src[1]) * k
            hz = 1.8
            legs = tuple(0.5 * _sin(hz, lt, i * math.pi / 2) for i in range(4))
            return _Pose(
                x,
                y,
                angle=1.5 * _sin(hz * 2, lt),
                legs=legs,  # type: ignore[arg-type]
                facing=1 if dst[0] >= src[0] else -1,
            )
        if b == "convulsion":
            hz = float(p.get("hz", 3.0))
            amp = float(p.get("amp", 0.012))
            fm = 0.4 * _sin(0.15, lt)  # slow frequency wander
            jitter = rng.normal(0, amp * 0.25, 2)
            legs = tuple(0.8 * _sin(hz, lt, i * math.pi / 2 + fm) for i in range(4))
            return _Pose(
                at[0] + amp * _sin(hz, lt, fm) + jitter[0],
                at[1] + 0.6 * amp * _sin(hz * 1.07, lt, 1.0) + jitter[1],
                angle=5.0 * _sin(hz, lt, 1.0 + fm),
                legs=legs,  # type: ignore[arg-type]
                head_dy=0.01 * _sin(hz, lt, 2.0),
                facing=p.get("facing", 1),
            )
        if b == "scratch":
            hz = float(p.get("hz", 5.0))
            return _Pose(
                at[0] + 0.002 * _sin(hz, lt),
                at[1],
                legs=(0.0, 0.0, 0.0, 0.9 * _sin(hz, lt)),
                head_dy=0.004 * _sin(hz, lt),
                facing=p.get("facing", 1),
            )
        if b == "shake_off":
            hz = float(p.get("hz", 4.5))
            return _Pose(
                at[0] + 0.006 * _sin(hz, lt),
                at[1],
                angle=14.0 * _sin(hz, lt),
                facing=p.get("facing", 1),
            )
        if b == "retch":
            hz = float(p.get("hz", 0.8))
            heave = 0.5 - 0.5 * math.cos(2.0 * math.pi * hz * lt)
            return _Pose(
                at[0],
                at[1] - 0.02 * heave,
                angle=-10.0 * heave,
                scale=1.0 + 0.06 * heave,
                head_dy=0.06 * heave,
                facing=p.get("facing", 1),
            )
        raise AssertionError(b)

    def _draw_subject(self, img: Image, pose: _Pose) -> None:
        if not pose.visible:
            return
        w, _h = self._size
        cx, cy = pose.x * w, pose.y * self._size[1]
        rx, ry = 0.085 * w * pose.scale, 0.04 * w * pose.scale
        f = pose.facing
        rad = math.radians(pose.angle)
        ca, sa = math.cos(rad), math.sin(rad)

        def rot(dx: float, dy: float) -> tuple[int, int]:
            return (round(cx + dx * ca - dy * sa), round(cy + dx * sa + dy * ca))

        leg_len = 0.045 * w
        for i, (bx, swing) in enumerate(zip((-0.7, -0.45, 0.45, 0.7), pose.legs, strict=True)):
            base = rot(bx * rx * f, ry * 0.6)
            end = (
                round(base[0] + leg_len * math.sin(swing + rad) * f),
                round(base[1] + leg_len * math.cos(swing + rad)),
            )
            cv2.line(img, base, end, (35, 55, 85) if i % 2 else (45, 70, 105), max(2, w // 90))
        cv2.ellipse(img, rot(0, 0), (round(rx), round(ry)), pose.angle, 0, 360, (60, 95, 145), -1)
        for k in range(-3, 4):  # fur stripes give interior texture for flow
            a, b = rot(k * rx * 0.22, -ry * 0.8), rot(k * rx * 0.22 + 0.1 * rx, ry * 0.8)
            cv2.line(img, a, b, (45, 75, 120), max(1, w // 200))
        head = rot(rx * 1.05 * f, -ry * 0.7 + pose.head_dy * w)
        cv2.circle(img, head, round(0.035 * w * pose.scale), (55, 85, 135), -1)
        cv2.circle(
            img,
            (head[0] + round(0.015 * w * f), head[1] - round(0.008 * w)),
            max(1, w // 160),
            (20, 20, 20),
            -1,
        )
        tail = rot(-rx * 1.05 * f, -ry * 0.3)
        cv2.line(
            img,
            tail,
            (tail[0] - round(0.04 * w * f), tail[1] - round(0.02 * w)),
            (50, 80, 125),
            max(2, w // 120),
        )

    def _apply_effects(self, img: Image, t: float) -> Image:
        w, h = self._size
        gain = 1.0
        shift = (0.0, 0.0)
        for e in self.scenario.effects:
            if e.kind == "camera_bump" and t >= e.start:
                d = e.params.get("shift", (6.0, 3.0))
                shift = (shift[0] + d[0], shift[1] + d[1])
            if not (e.start <= t < e.end):
                continue
            lt = t - e.start
            if e.kind == "lighting_step":
                gain *= float(e.params.get("level", 0.6))
            elif e.kind == "lighting_ramp":
                gain *= 1.0 + (float(e.params.get("level", 0.6)) - 1.0) * lt / (e.end - e.start)
            elif e.kind == "camera_shake":
                amp = float(e.params.get("amp", 8.0))
                hz = float(e.params.get("hz", 3.0))
                shift = (shift[0] + amp * _sin(hz, lt), shift[1] + 0.5 * amp * _sin(hz, lt, 1.3))
            elif e.kind == "person":
                k = lt / (e.end - e.start)
                px = int((-0.1 + 1.2 * k) * w)
                stride = int(0.03 * w * _sin(1.0, lt))
                cv2.rectangle(
                    img,
                    (px - int(0.05 * w), int(0.1 * h)),
                    (px + int(0.05 * w), int(0.7 * h)),
                    (50, 50, 60),
                    -1,
                )
                cv2.line(img, (px, int(0.7 * h)), (px - stride, int(0.98 * h)), (40, 40, 50), 14)
                cv2.line(img, (px, int(0.7 * h)), (px + stride, int(0.98 * h)), (40, 40, 50), 14)
                cv2.circle(img, (px, int(0.06 * h)), int(0.04 * w), (90, 110, 150), -1)
        if shift != (0.0, 0.0):
            m = np.array([[1, 0, shift[0]], [0, 1, shift[1]]], dtype=np.float32)
            img = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REFLECT)
        if gain != 1.0:
            img = cv2.convertScaleAbs(img, alpha=gain)
        return img

    def render(self, t: float, rng: np.random.Generator) -> Image:
        img = self._background.copy()
        self._draw_subject(img, self._pose(t, rng))
        img = self._apply_effects(img, t)
        if self._noise_bank:
            noise = self._noise_bank[int(rng.integers(len(self._noise_bank)))]
            img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        return img

    def frames(self) -> Iterator[Frame]:
        rng = np.random.default_rng(self._seed + 1)
        n = int(self.scenario.duration * self._fps)
        for i in range(n):
            t = i / self._fps
            yield Frame(i, t, self.render(t, rng))


# --------------------------------------------------------------------- presets


def _rest(a: float, b: float, at: Point = (0.5, 0.65)) -> Segment:
    return Segment(a, b, "rest", {"at": at})


def _preset_rest() -> Scenario:
    return Scenario("rest", "Subject resting/sleeping with slow breathing.", 60.0, (_rest(0, 60),))


def _preset_convulsion() -> Scenario:
    return Scenario(
        "convulsion",
        "Rest, then 60 s of sustained rhythmic convulsion-like shaking.",
        120.0,
        (_rest(0, 20), Segment(20, 80, "convulsion", {"at": (0.5, 0.65)}), _rest(80, 120)),
    )


def _preset_walk() -> Scenario:
    return Scenario(
        "walk",
        "Subject walking back and forth across the room.",
        60.0,
        (
            Segment(0, 15, "walk", {"from": (0.15, 0.65), "to": (0.85, 0.7)}),
            Segment(15, 30, "walk", {"from": (0.85, 0.7), "to": (0.15, 0.62)}),
            Segment(30, 45, "walk", {"from": (0.15, 0.62), "to": (0.85, 0.68)}),
            Segment(45, 60, "walk", {"from": (0.85, 0.68), "to": (0.2, 0.65)}),
        ),
    )


def _preset_scratch() -> Scenario:
    segs: list[Segment] = []
    t = 0.0
    for burst in (4.0, 5.0, 3.0, 6.0):
        segs += [_rest(t, t + 10), Segment(t + 10, t + 10 + burst, "scratch", {"at": (0.5, 0.65)})]
        t += 10 + burst
    segs.append(_rest(t, t + 10))
    return Scenario("scratch", "Rest with short scratching bursts (3–6 s).", t + 10, tuple(segs))


def _preset_shake_off() -> Scenario:
    segs: list[Segment] = []
    for k in range(4):
        t = k * 15.0
        segs += [_rest(t, t + 13), Segment(t + 13, t + 15, "shake_off", {"at": (0.5, 0.65)})]
    return Scenario("shake_off", "Rest with 2 s full-body shake-offs.", 60.0, tuple(segs))


def _preset_lighting() -> Scenario:
    return Scenario(
        "lighting",
        "Resting subject with a lamp switching and a gradual dimming.",
        60.0,
        (_rest(0, 60),),
        (
            Effect(10, 25, "lighting_step", {"level": 0.6}),
            Effect(35, 55, "lighting_ramp", {"level": 0.5}),
        ),
    )


def _preset_camera() -> Scenario:
    return Scenario(
        "camera",
        "Resting subject; camera is shaken for 4 s and later knocked.",
        60.0,
        (_rest(0, 60),),
        (
            Effect(15, 19, "camera_shake", {"amp": 8.0, "hz": 3.0}),
            Effect(40, 41, "camera_bump", {"shift": (5.0, 2.0)}),
        ),
    )


def _preset_person() -> Scenario:
    return Scenario(
        "person",
        "Resting subject while a person walks through the frame twice.",
        60.0,
        (_rest(0, 60),),
        (Effect(10, 18, "person"), Effect(35, 43, "person")),
    )


def _preset_retch() -> Scenario:
    return Scenario(
        "retch",
        "Rest, 10 s of slow retching-like heaving, rest.",
        50.0,
        (_rest(0, 15), Segment(15, 25, "retch", {"at": (0.5, 0.65)}), _rest(25, 50)),
    )


def _preset_cluster() -> Scenario:
    return Scenario(
        "cluster",
        "Two convulsion episodes 60 s apart (repeat episode must alert again).",
        200.0,
        (
            _rest(0, 15),
            Segment(15, 65, "convulsion", {"at": (0.5, 0.65)}),
            _rest(65, 125),
            Segment(125, 170, "convulsion", {"at": (0.5, 0.65), "hz": 2.5}),
            _rest(170, 200),
        ),
    )


def _preset_pause() -> Scenario:
    return Scenario(
        "pause",
        "Convulsion with a 5 s pause midway (must stay one incident).",
        100.0,
        (
            _rest(0, 15),
            Segment(15, 45, "convulsion", {"at": (0.5, 0.65)}),
            _rest(45, 50),
            Segment(50, 80, "convulsion", {"at": (0.5, 0.65)}),
            _rest(80, 100),
        ),
    )


def _preset_mixed() -> Scenario:
    return Scenario(
        "mixed",
        "A compressed 'day': walking, resting, scratching, shake-off, person, "
        "lighting change, then a 70 s convulsion and recovery.",
        240.0,
        (
            Segment(0, 12, "walk", {"from": (0.15, 0.65), "to": (0.6, 0.66)}),
            _rest(12, 40, (0.6, 0.66)),
            Segment(40, 44, "scratch", {"at": (0.6, 0.66)}),
            _rest(44, 70, (0.6, 0.66)),
            Segment(70, 72, "shake_off", {"at": (0.6, 0.66)}),
            _rest(72, 130, (0.6, 0.66)),
            Segment(130, 200, "convulsion", {"at": (0.6, 0.66), "hz": 2.2}),
            _rest(200, 240, (0.6, 0.66)),
        ),
        (Effect(50, 58, "person"), Effect(90, 110, "lighting_step", {"level": 0.7})),
    )


def _preset_handheld() -> Scenario:
    return Scenario(
        "handheld",
        "Handheld phone footage: constant slow hand sway, a 60 s convulsion. "
        "Needs stabilization (config/handheld.toml).",
        100.0,
        (_rest(0, 20), Segment(20, 80, "convulsion", {"at": (0.5, 0.65)}), _rest(80, 100)),
        (Effect(0, 100, "camera_shake", {"amp": 6.0, "hz": 0.4}),),
        fixed_camera=False,
    )


PRESETS: dict[str, Callable[[], Scenario]] = {
    "rest": _preset_rest,
    "convulsion": _preset_convulsion,
    "walk": _preset_walk,
    "scratch": _preset_scratch,
    "shake_off": _preset_shake_off,
    "lighting": _preset_lighting,
    "camera": _preset_camera,
    "person": _preset_person,
    "retch": _preset_retch,
    "cluster": _preset_cluster,
    "pause": _preset_pause,
    "mixed": _preset_mixed,
    "handheld": _preset_handheld,
}


def get_scenario(name: str) -> Scenario:
    try:
        return PRESETS[name]()
    except KeyError:
        raise KeyError(f"unknown scenario {name!r} (known: {', '.join(PRESETS)})") from None
