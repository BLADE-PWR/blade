"""Which model runs a stream in "auto" mode. Each model package says in model.yaml (`scale`) at what object size it
works: {object_m, min_px, max_px}, the pixels an object of object_m metres at the rangefinder point takes at the
model's input. The routing (an ordered set of models, optional per-model overrides of min_px / max_px) is what the
operator sets; the first model whose range holds the frame runs it, and none runs it when no range does."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .ground import px_on_target
from .package import ModelPackage

OBJECT_M = 1.8  # a standing soldier: the default reference object of a model without scale.object_m
MARGIN = 0.15
HOLD_S = 1.0


@dataclass
class Entry:
    model: str
    min_px: float | None = None
    max_px: float | None = None


@dataclass
class Routing:
    models: list[Entry] = field(default_factory=list)
    margin: float = MARGIN  # the running model keeps the stream while its range, widened by this fraction, holds it
    hold_s: float = HOLD_S  # a model (or none) takes over only after the frame stays outside the running one this long

    @classmethod
    def from_dict(cls, d: dict) -> Routing:
        r = cls(models=[Entry(e["model"], e.get("min_px"), e.get("max_px")) for e in d.get("models", [])],
                margin=float(d.get("margin", MARGIN)), hold_s=float(d.get("hold_s", HOLD_S)))
        if not 0 <= r.margin < 1 or r.hold_s < 0:
            raise ValueError("margin must be in [0, 1) and hold_s >= 0")
        if len({e.model for e in r.models}) != len(r.models):
            raise ValueError("a model is listed twice")
        for e in r.models:
            if e.min_px is not None and e.max_px is not None and not e.min_px < e.max_px:
                raise ValueError(f"{e.model}: min_px must be below max_px")
        return r

    def to_dict(self) -> dict:
        return {"models": [{k: v for k, v in (("model", e.model), ("min_px", e.min_px), ("max_px", e.max_px))
                            if v is not None} for e in self.models],
                "margin": self.margin, "hold_s": self.hold_s}


def scale_range(pkg: ModelPackage, entry: Entry) -> tuple[float, float, float]:
    """(object_m, min_px, max_px) of a model in the routing: the entry's override, else model.yaml, else open."""
    sc = pkg.scale
    lo = entry.min_px if entry.min_px is not None else sc.get("min_px")
    hi = entry.max_px if entry.max_px is not None else sc.get("max_px")
    return float(sc.get("object_m", OBJECT_M)), float(lo or 0.0), math.inf if hi is None else float(hi)


def describe(routing: Routing, packages: dict[str, ModelPackage]) -> dict:
    """The routing as the operator sees it: per model the range in force, what model.yaml says, whether it is
    overridden, and whether the package is loaded."""
    out = []
    for e in routing.models:
        pkg = packages.get(e.model)
        row: dict = {"model": e.model, "loaded": pkg is not None}
        if pkg is not None:
            obj, lo, hi = scale_range(pkg, e)
            row |= {"object_m": obj, "min_px": lo, "max_px": None if math.isinf(hi) else hi,
                    "yaml": {"min_px": pkg.scale.get("min_px"), "max_px": pkg.scale.get("max_px")},
                    "overridden": e.min_px is not None or e.max_px is not None}
        out.append(row)
    return {"models": out, "margin": routing.margin, "hold_s": routing.hold_s}


@dataclass
class Choice:
    model: str | None
    px: float | None


@dataclass
class _Stream:
    choice: Choice
    waiting: bool = False  # the frame has pointed to `pending` instead of the running model since `since`
    pending: str | None = None
    since: float = 0.0


def _rows(routing: Routing, packages: dict[str, ModelPackage], tel: dict, img_w: int, img_h: int) -> list[tuple]:
    rows = []  # (model id, px, lo, hi)
    for e in routing.models:
        pkg = packages.get(e.model)
        if pkg is not None:
            obj, lo, hi = scale_range(pkg, e)
            rows.append((e.model, px_on_target(tel, obj, pkg.imgsz_max, img_w, img_h), lo, hi))
    return rows


class Router:
    """Per stream state of the auto choice: which model runs, and what is waiting to replace it."""

    def __init__(self):
        self.streams: dict[str, _Stream] = {}

    def forget(self, sid: str) -> None:
        self.streams.pop(sid, None)

    def current(self, sid: str) -> Choice | None:
        s = self.streams.get(sid)
        return s.choice if s else None

    def choose(self, routing: Routing, packages: dict[str, ModelPackage], sid: str, tel: dict, img_w: int,
               img_h: int, t: float) -> Choice:
        rows = _rows(routing, packages, tel, img_w, img_h)
        s = self.streams.get(sid)
        if not rows or math.isnan(rows[0][1]):  # nothing to run, or no range: the last choice stays
            return s.choice if s else Choice(rows[0][0] if rows else None, None)
        first = next((m for m, px, lo, hi in rows if lo <= px <= hi), None)
        if s is None:
            s = self.streams[sid] = _Stream(Choice(first, rows[0][1]))
            return s.choice
        s.choice = self._settle(s, routing, rows, first, t)
        return s.choice

    @staticmethod
    def _settle(s: _Stream, routing: Routing, rows: list[tuple], first: str | None, t: float) -> Choice:
        """The running model stays while its range, widened by the margin, holds the frame; another (or none) takes
        over after the frame has pointed to it for hold_s."""
        by_model = {m: (px, lo, hi) for m, px, lo, hi in rows}
        cur = s.choice.model
        if cur in by_model:
            px, lo, hi = by_model[cur]
            if lo * (1 - routing.margin) <= px <= hi * (1 + routing.margin):
                s.waiting = False
                return Choice(cur, px)
        elif first is None and cur is None:  # still no model for the scene
            s.waiting = False
            return Choice(None, rows[0][1])
        if not s.waiting or s.pending != first:
            s.waiting, s.pending, s.since = True, first, t
        if t - s.since >= routing.hold_s:
            s.waiting = False
            return Choice(first, by_model[first][0] if first else rows[0][1])
        return Choice(cur, by_model[cur][0] if cur in by_model else rows[0][1])  # still waiting
