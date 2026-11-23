"""Force groups: objects close to each other, whatever their class, for the operator's map. Not alerts: no
rule, no severity. Same single-linkage grouping as the vehicle_group rule."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

PAD_M = 20.0  # m, the outline reaches this far around the outermost objects (so one object has an area too)
PAD_POINTS = 12


@dataclass
class Params:
    radius_m: float = 150.0
    min_count: int = 2
    max_age_s: float = 60.0  # stale objects count as long as this since they were last seen


def link(objs: list[dict], radius_m: float) -> list[list[dict]]:
    """Single linkage: objects within radius_m of one another, directly or through others."""
    seen: set[str] = set()
    groups = []
    for o in objs:
        if o["oid"] in seen:
            continue
        group, todo = [], [o]
        seen.add(o["oid"])
        while todo:
            a = todo.pop()
            group.append(a)
            for b in objs:
                if b["oid"] not in seen and math.dist(a["xy"], b["xy"]) <= radius_m:
                    seen.add(b["oid"])
                    todo.append(b)
        groups.append(group)
    return groups


def mean_vel(vels: list[tuple[float, float]]) -> tuple[float, float] | None:
    return (sum(v[0] for v in vels) / len(vels), sum(v[1] for v in vels) / len(vels)) if vels else None


def same_group(a: set[str], b: set[str]) -> bool:
    """A group whose members changed a little is the same group: at least half of the larger one is shared."""
    return len(a & b) * 2 >= max(len(a), len(b))


def convex_hull(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain, counter-clockwise."""
    pts = sorted(set(pts))
    if len(pts) < 3:
        return pts

    def half(points):
        out: list[tuple[float, float]] = []
        for p in points:
            while len(out) >= 2 and (out[-1][0] - out[-2][0]) * (p[1] - out[-2][1]) \
                    - (out[-1][1] - out[-2][1]) * (p[0] - out[-2][0]) <= 0:
                out.pop()
            out.append(p)
        return out[:-1]

    return half(pts) + half(reversed(pts))


def area(poly: list[tuple[float, float]]) -> float:
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(poly, poly[1:] + poly[:1]))) / 2


def outline(xys: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ring = [(PAD_M * math.cos(2 * math.pi * k / PAD_POINTS), PAD_M * math.sin(2 * math.pi * k / PAD_POINTS))
            for k in range(PAD_POINTS)]
    return convex_hull([(x + dx, y + dy) for x, y in xys for dx, dy in ring])


def describe(cid: str, members: list[dict]) -> dict:
    xys = [tuple(o["xy"]) for o in members]
    hull = outline(xys)
    comp: dict[str, int] = {}
    for o in members:
        comp[o["grp"]] = comp.get(o["grp"], 0) + 1
    vel = mean_vel([o["vel"] for o in members if o.get("vel")])
    return {"cid": cid, "objects": sorted(o["oid"] for o in members), "composition": comp,
            "centre": (round(sum(x for x, _ in xys) / len(xys), 1), round(sum(y for _, y in xys) / len(xys), 1)),
            "hull": [(round(x, 1), round(y, 1)) for x, y in hull], "area_m2": round(area(hull)),
            "vel": None if vel is None else (round(vel[0], 2), round(vel[1], 2))}


@dataclass
class Grouper:
    """Groups a world's objects and keeps a group's id while its members change a little."""
    params: Params = field(default_factory=Params)
    prev: dict[str, set[str]] = field(default_factory=dict)  # cid -> members at the last update
    made: int = 0  # group ids given so far

    def update(self, objects: dict[str, dict], t: float) -> list[dict]:
        p = self.params
        near = [o for o in objects.values() if o.get("state") == "active" or t - o["last"] <= p.max_age_s]
        groups = [g for g in link(near, p.radius_m) if len(g) >= p.min_count]
        free = dict(self.prev)
        out, now = [], {}
        for g in sorted(groups, key=len, reverse=True):
            ids = {o["oid"] for o in g}
            best = max((c for c, m in free.items() if same_group(ids, m)), key=lambda c: len(ids & free[c]),
                       default=None)
            if best is None:
                self.made += 1
            cid = best if best is not None else f"g-{self.made:04d}"
            free.pop(cid, None)
            now[cid] = ids
            out.append(describe(cid, g))
        self.prev = now
        return out
