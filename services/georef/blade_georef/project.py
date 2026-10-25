"""Pixel -> terrain for one frame: every box's anchor, the image corners and the optical axis. Soldiers
use the box's bottom-centre; vehicles the box centre against terrain raised by half their height
(a vehicle box's bottom edge is its near side, which differs per drone)."""
from __future__ import annotations

import math

import numpy as np

from blade_geometry import Camera, Terrain, ground_sigma

CENTRE_LIFT_M = {"tank": 1.3, "military_vehicle": 1.2}  # half the height of typical Arma vehicles


def anchor(d: dict) -> tuple[float, float, float]:
    """-> pixel (u, v) and the height above terrain the ray is cut at."""
    x1, y1, x2, y2 = d["box"]
    lift = CENTRE_LIFT_M.get(d["grp"])
    if lift is None:
        return (x1 + x2) / 2, y2, 0.0
    return (x1 + x2) / 2, (y1 + y2) / 2, lift


def project_frame(terrain: Terrain, tel: dict, dets: list[dict]) -> dict:
    cam = Camera(tel)
    W, H = cam.W, cam.H
    anchors = [anchor(d) for d in dets]
    px = [(u, v) for u, v, _ in anchors] + [(0, 0), (W, 0), (W, H), (0, H), (W / 2, H / 2)]
    lifts = np.array([lift for _, _, lift in anchors] + [0.0] * 5)
    arr = np.asarray(px, dtype=np.float64)
    rays = cam.rays(arr[:, 0], arr[:, 1])
    hits = np.full((len(px), 3), np.nan)
    for lift in np.unique(lifts):  # lowering the camera = raising the terrain
        sel = lifts == lift
        h = terrain.intersect(cam.o - np.array([0.0, 0.0, lift]), rays[sel])
        h[:, 2] = terrain.height(h[:, 0], h[:, 1]) if lift else h[:, 2]
        hits[sel] = h
    n = len(dets)
    pts = []
    for d, ray, hit in zip(dets, rays[:n], hits[:n]):
        p = {"tid": d.get("tid"), "cls": d["cls"], "grp": d["grp"], "conf": d["conf"]}
        if np.isfinite(hit[0]):
            slant = float(np.linalg.norm(hit - cam.o))
            p["xyz"] = [round(float(v), 2) for v in hit]
            p["sigma_m"] = round(ground_sigma(slant, math.asin(min(1.0, -ray[2])) if ray[2] < 0 else 0.0,
                                              cam.ifov_rad(), d["box"][3] - d["box"][1], d["grp"]), 2)
        pts.append(p)
    footprint = [[round(float(h[0]), 1), round(float(h[1]), 1)] if np.isfinite(h[0]) else None
                 for h in hits[n:n + 4]]
    centre = hits[n + 4]
    out = {"camera": [round(float(v), 2) for v in cam.o], "footprint": footprint, "pts": pts}
    if np.isfinite(centre[0]):
        out["centre"] = [round(float(v), 2) for v in centre]
        out["gsd_m"] = round(float(np.linalg.norm(centre - cam.o)) * cam.ifov_rad(), 4)
        if cam.range_m:
            out["range_check_m"] = round(abs(float(np.linalg.norm(centre - cam.o)) - cam.range_m), 2)
    return out
