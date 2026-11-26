"""Which model runs a stream in "auto" mode. Each model package says in model.yaml (`scale`) at what object size it
works: {object_m, min_px, max_px}, the pixels an object of object_m metres at the rangefinder point takes at the
model's input. The routing (an ordered set of models, optional per-model overrides of min_px / max_px) is what the
operator sets; the first model whose range holds the frame runs it, and none runs it when no range does."""
from __future__ import annotations

import math
from dataclasses import dataclass

from pydantic import BaseModel, Field, field_validator

from .ground import px_on_target
from .package import ModelPackage

OBJECT_M = 1.8  # a standing soldier: the default reference object of a model without scale.object_m
MARGIN = 0.15
HOLD_S = 1.0


class Entry(BaseModel):
    model: str
    min_px: float | None = Field(None, ge=0, description="overrides scale.min_px of model.yaml")
    max_px: float | None = Field(None, ge=0, description="overrides scale.max_px of model.yaml")


class Routing(BaseModel):
    """The body of PUT /routing and what KV "routing" holds. Whether a range is empty depends on model.yaml, so
    PUT /routing checks that against the packages."""
    models: list[Entry] = []
    # the running model keeps the stream while its range, widened by this fraction, holds it
    margin: float = Field(MARGIN, ge=0, lt=1)
    # a model (or none) takes over only after the frame stays outside the running one this long
    hold_s: float = Field(HOLD_S, ge=0)

    @field_validator("models")
    @classmethod
    def _once(cls, models: list[Entry]) -> list[Entry]:
        if len({e.model for e in models}) != len(models):
            raise ValueError("a model is listed twice")
        return models


def scale_range(pkg: ModelPackage, entry: Entry) -> tuple[float, float, float]:
    """(object_m, min_px, max_px) of a model in the routing: the entry's override, else model.yaml, else open."""
    sc = pkg.scale
    lo = entry.min_px if entry.min_px is not None else sc.get("min_px")
    hi = entry.max_px if entry.max_px is not None else sc.get("max_px")
    return float(sc.get("object_m", OBJECT_M)), float(lo or 0.0), math.inf if hi is None else float(hi)


def describe(routing: Routing, packages: dict[str, ModelPackage]) -> dict:
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
    pending: tuple[str | None, float] | None = None  # (model, since when) the frame points to instead of the running


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
                s.pending = None
                return Choice(cur, px)
        elif first is None and cur is None:  # still no model for the scene
            s.pending = None
            return Choice(None, rows[0][1])
        if s.pending is None or s.pending[0] != first:
            s.pending = (first, t)
        if t - s.pending[1] >= routing.hold_s:
            s.pending = None
            return Choice(first, by_model[first][0] if first else rows[0][1])
        return Choice(cur, by_model[cur][0] if cur in by_model else rows[0][1])  # still waiting
