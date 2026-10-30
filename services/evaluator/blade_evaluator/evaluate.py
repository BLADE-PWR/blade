"""Localisation error against sim truth for objects inside the camera footprint: `geo` (every georef
point) and `world` (every active world object). Greedy matching inside a per-group gate;
CEP50 / CEP90 = median / 90th percentile."""
from __future__ import annotations

import math
from collections import OrderedDict, defaultdict, deque

import numpy as np

from blade_common.util import inside

GATE_M = {"soldier": 10.0, "tank": 20.0, "military_vehicle": 20.0}
TRUTH_GROUP = {"soldier": "soldier", "tank": "tank", "wheeled_vehicle": "military_vehicle",
               "military_vehicle": "military_vehicle", "apc": "military_vehicle", "truck": "military_vehicle"}


def match(truth: list[tuple], est: list[tuple]) -> tuple[list[tuple], set, set]:
    """truth [(tid, grp, (x, y))], est [(key, grp, (x, y))] -> pairs [(tid, key, err)], unmatched truth / est"""
    cands = []
    for ti, (_, tg, txy) in enumerate(truth):
        for ei, (_, eg, exy) in enumerate(est):
            if tg == eg:
                d = math.dist(txy, exy)
                if d <= GATE_M.get(tg, 15.0):
                    cands.append((d, ti, ei))
    used_t, used_e, pairs = set(), set(), []
    for d, ti, ei in sorted(cands):
        if ti not in used_t and ei not in used_e:
            used_t.add(ti)
            used_e.add(ei)
            pairs.append((truth[ti][0], est[ei][0], d))
    return pairs, set(range(len(truth))) - used_t, set(range(len(est))) - used_e


def cep(errs) -> tuple[float | None, float | None]:
    if not len(errs):
        return None, None
    c50, c90 = np.percentile(np.asarray(errs, dtype=float), [50, 90])
    return round(float(c50), 2), round(float(c90), 2)


class StreamTruth:
    def __init__(self):
        self.cls: dict[int, str] = {}
        self.frames: OrderedDict[int, dict[int, tuple]] = OrderedDict()  # frame -> id -> (x, y)
        self.footprint: list | None = None

    def spawn(self, rec: dict) -> None:
        self.cls[int(rec["id"])] = rec.get("cls", "")

    def gt(self, rec: dict) -> None:
        self.frames[int(rec["frame"])] = {int(o[0]): (float(o[1][0]), float(o[1][1])) for o in rec["objs"]}
        while len(self.frames) > 120:
            self.frames.popitem(last=False)

    def at(self, frame: int | None) -> dict[int, tuple] | None:
        if not self.frames:
            return None
        if frame is None:
            return next(reversed(self.frames.values()))
        best = max((f for f in self.frames if f <= frame), default=None)
        return self.frames[best] if best is not None else None

    def in_view(self, frame: int | None = None) -> list[tuple]:
        """[(truth id, group, (x, y))] for known objects inside the latest footprint."""
        objs = self.at(frame)
        if not objs or not self.footprint:
            return []
        out = []
        for oid, xy in objs.items():
            grp = TRUTH_GROUP.get(self.cls.get(oid, ""))
            if grp and inside(self.footprint, *xy):
                out.append((oid, grp, xy))
        return out


class WorldEval:
    def __init__(self, wid: str, window_s: float = 30.0):
        self.wid = wid
        self.window_s = window_s
        self.samples: dict[str, dict[str, deque]] = {"geo": defaultdict(deque), "world": defaultdict(deque)}
        self.totals: dict[str, dict[str, list]] = {"geo": defaultdict(list), "world": defaultdict(list)}
        self.detect = defaultdict(lambda: [0, 0])  # grp -> [matched, in view], cumulative

    def add(self, level: str, grp: str, err: float, t: float) -> None:
        self.samples[level][grp].append((t, err))
        self.totals[level][grp].append(err)

    def trim(self, t: float) -> None:
        for level in self.samples.values():
            for q in level.values():
                while q and t - q[0][0] > self.window_s:
                    q.popleft()

    def summary(self, level: str = "world") -> dict[str, dict]:
        out = {}
        for grp, q in self.samples[level].items():
            c50, c90 = cep([e for _, e in q])
            out[grp] = {"n": len(q), "cep50_m": c50, "cep90_m": c90}
        return out

    def report(self) -> dict:
        rep = {}
        for level, groups in self.totals.items():
            rep[level] = {g: dict(zip(("cep50_m", "cep90_m"), cep(e)), n=len(e)) for g, e in groups.items()}
        rep["detection"] = {g: {"matched": m, "in_view": v, "rate": round(m / v, 3) if v else None}
                            for g, (m, v) in self.detect.items()}
        return rep
