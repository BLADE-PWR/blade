"""Attention cues over the world state. No rule ranks targets or suggests an action: an alert says where
to look and why. A rule is (WorldState, params, now) -> [Cue]; a cue's key is stable."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from blade_common.util import inside

from .clusters import link

VEHICLES = {"tank", "military_vehicle"}

DEFAULTS: dict[str, dict] = {
    "vehicle_group": {"enabled": True, "min_count": 3, "radius_m": 200.0},
    "lost_contact": {"enabled": True, "after_s": 60.0, "min_obs": 30,
                     "groups": ["tank", "military_vehicle", "soldier"]},
    "uncertain_position": {"enabled": True, "sigma_m": 15.0},
    "infantry_resolution": {"enabled": True, "gsd_m": 0.06, "hold_s": 10.0},
    "explosion_near": {"enabled": True, "radius_m": 100.0},
    "zone": {"enabled": True, "zones": [], "eta_s": 60.0,
             "groups": ["tank", "military_vehicle", "soldier"]},
}


@dataclass
class Cue:
    key: str
    rule: str
    severity: str
    title: str
    explanation: str
    objects: list[str] = field(default_factory=list)
    confidence: float | None = None
    xy: tuple[float, float] | None = None


@dataclass
class WorldState:
    objects: dict[str, dict] = field(default_factory=dict)  # oid -> latest object (active / stale)
    gone: dict[str, dict] = field(default_factory=dict)  # oid -> last state of expired objects
    events: list[dict] = field(default_factory=list)  # recent world events
    cameras: dict[str, dict] = field(default_factory=dict)  # sid -> {"gsd_m", "centre", "t"}


def _conf(objs: list[dict]) -> float:
    """More observations and a smaller sigma -> closer to 1."""
    if not objs:
        return 0.0
    vals = [min(1.0, o.get("n_obs", 1) / 30) * (1 / (1 + (o.get("sigma_m") or 5) / 10)) for o in objs]
    return round(sum(vals) / len(vals), 2)


def vehicle_group(ws: WorldState, p: dict, t: float) -> list[Cue]:
    veh = [o for o in ws.objects.values() if o["grp"] in VEHICLES and o.get("state") == "active"]
    out = []
    for cluster in link(veh, p["radius_m"]):
        if len(cluster) >= p["min_count"]:
            ids = sorted(c["oid"] for c in cluster)
            cx = sum(c["xy"][0] for c in cluster) / len(cluster)
            cy = sum(c["xy"][1] for c in cluster) / len(cluster)
            kinds = sorted({c["grp"] for c in cluster})
            out.append(Cue(f"vehicle_group:{ids[0]}", "vehicle_group", "notice",
                           f"{len(cluster)} vehicles together",
                           f"{len(cluster)} vehicles ({', '.join(kinds)}) within {p['radius_m']:.0f} m of each other.",
                           ids, _conf(cluster), (round(cx, 1), round(cy, 1))))
    return out


def lost_contact(ws: WorldState, p: dict, t: float) -> list[Cue]:
    out = []
    for o in list(ws.objects.values()) + list(ws.gone.values()):
        age = t - o["last"]
        if o["grp"] in p["groups"] and o.get("n_obs", 0) >= p["min_obs"] and age >= p["after_s"]:
            out.append(Cue(f"lost_contact:{o['oid']}", "lost_contact", "info", f"{o['grp']} {o['oid']} not seen",
                           f"Last seen {age:.0f} s ago at ({o['xy'][0]:.0f}, {o['xy'][1]:.0f}), "
                           f"after {o['n_obs']} observations.", [o["oid"]], _conf([o]), tuple(o["xy"])))
    return out


def uncertain_position(ws: WorldState, p: dict, t: float) -> list[Cue]:
    out = []
    for o in ws.objects.values():
        s = o.get("sigma_m")
        if o.get("state") == "active" and s and s > p["sigma_m"]:
            out.append(Cue(f"uncertain_position:{o['oid']}", "uncertain_position", "info",
                           f"{o['grp']} {o['oid']}: position uncertain",
                           f"Expected error {s:.0f} m: seen only from a flat angle or far away. "
                           "Another view of this area would fix it.", [o["oid"]], _conf([o]), tuple(o["xy"])))
    return out


def infantry_resolution(ws: WorldState, p: dict, t: float) -> list[Cue]:
    out = []
    for sid, cam in ws.cameras.items():
        g = cam.get("gsd_m")
        if g and g > p["gsd_m"] and t - cam["t"] < p["hold_s"]:
            px = 1.8 / g
            out.append(Cue(f"infantry_resolution:{sid}", "infantry_resolution", "notice",
                           f"{sid}: infantry may be missed",
                           f"{g:.3f} m per pixel: a standing soldier is about {px:.0f} px tall, below what the "
                           "detector finds reliably. Zoom in to check this area for infantry.",
                           [], None, tuple(cam["centre"][:2]) if cam.get("centre") else None))
    return out


def explosion_near(ws: WorldState, p: dict, t: float) -> list[Cue]:
    out = []
    for e in ws.events:
        if e["kind"] != "explosion" or t - e["t"] > e.get("ttl_s", 5):
            continue
        near = [o for o in ws.objects.values() if math.dist(o["xy"], e["xy"]) <= p["radius_m"]]
        if near:
            out.append(Cue(f"explosion_near:{e['eid']}", "explosion_near", "warning", "Explosion near known objects",
                           f"Explosion at ({e['xy'][0]:.0f}, {e['xy'][1]:.0f}), {len(near)} known object(s) within "
                           f"{p['radius_m']:.0f} m.", sorted(o["oid"] for o in near), None, tuple(e["xy"])))
    return out


def zone(ws: WorldState, p: dict, t: float) -> list[Cue]:
    out = []
    for z in p["zones"]:
        poly = z["polygon"]
        for o in ws.objects.values():
            if o["grp"] not in p["groups"] or o.get("state") != "active":
                continue
            x, y = o["xy"]
            if inside(poly, x, y):
                out.append(Cue(f"zone:{z['name']}:{o['oid']}", "zone", "notice", f"{o['grp']} in {z['name']}",
                               f"{o['grp']} {o['oid']} is inside the marked area {z['name']}.", [o["oid"]],
                               _conf([o]), (x, y)))
                continue
            vx, vy = o.get("vel") or (0.0, 0.0)
            speed = math.hypot(vx, vy)
            if speed < 0.5:
                continue
            for k in range(2, int(p["eta_s"]) + 1, 2):  # the straight-line path ahead, every 2 s
                if inside(poly, x + vx * k, y + vy * k):
                    out.append(Cue(f"zone:{z['name']}:{o['oid']}:approach", "zone", "notice",
                                   f"{o['grp']} heading into {z['name']}",
                                   f"{o['grp']} {o['oid']} moves at {speed:.1f} m/s towards the marked area "
                                   f"{z['name']}; at this speed it reaches it in about {k} s.",
                                   [o["oid"]], _conf([o]), (x, y)))
                    break
    return out


RULES = {"vehicle_group": vehicle_group, "lost_contact": lost_contact, "uncertain_position": uncertain_position,
         "infantry_resolution": infantry_resolution, "explosion_near": explosion_near, "zone": zone}
