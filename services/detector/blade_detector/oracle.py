"""MODEL=oracle: boxes from sim truth. Each object's 3D box is projected with the frame's camera and its
id is the track id: the geometry-only error, no occlusion."""
from __future__ import annotations

import math
from collections import OrderedDict

import numpy as np

from blade_geometry import Camera

STANCE_CLS = {"STAND": "soldier_standing", "CROUCH": "soldier_kneeling", "PRONE": "soldier_prone",
              "": "soldier_standing"}
STANCE_H = {"STAND": 1.0, "CROUCH": 0.6, "PRONE": 0.3, "": 1.0}
VEHICLE_CLS = {"tank": "tank", "wheeled_vehicle": "military_vehicle", "military_vehicle": "military_vehicle",
               "apc": "military_vehicle", "truck": "military_vehicle"}


class Oracle:
    id = "oracle@1"

    def __init__(self):
        self.registry: dict[str, dict[int, dict]] = {}
        self.gt: dict[str, OrderedDict[int, list]] = {}

    def on_spawn(self, sid: str, rec: dict) -> None:
        self.registry.setdefault(sid, {})[int(rec["id"])] = rec

    def on_gt(self, sid: str, rec: dict) -> None:
        q = self.gt.setdefault(sid, OrderedDict())
        q[int(rec["frame"])] = rec["objs"]
        while len(q) > 64:
            q.popitem(last=False)

    def detect(self, sid: str, tel: dict) -> list[dict]:
        q = self.gt.get(sid)
        reg = self.registry.get(sid, {})
        if not q:
            return []
        frame = tel["frame"]
        best = max((f for f in q if f <= frame), default=None)
        objs = q[best] if best is not None else q[next(iter(q))]
        cam = Camera(tel)
        W, H = cam.W, cam.H
        out = []
        for obj_id, pos, dir_deg, _vel, stance in objs:
            sp = reg.get(int(obj_id))
            if sp is None:
                continue
            kind = sp.get("cls", "")
            if kind == "soldier":
                cls, grp = STANCE_CLS.get(stance or "", "soldier_standing"), "soldier"
            elif kind in VEHICLE_CLS:
                cls = grp = VEHICLE_CLS[kind]
            else:
                continue
            lo = np.asarray(sp.get("bbox_min") or [-0.5, -0.5, -1], float)
            hi = np.asarray(sp.get("bbox_max") or [0.5, 0.5, 1], float)
            hz = (hi[2] - lo[2]) * (STANCE_H.get(stance or "", 1.0) if kind == "soldier" else 1.0)
            xs, ys = (lo[0], hi[0]), (lo[1], hi[1])
            if kind == "soldier" and stance == "PRONE":
                ys = (lo[1] * 3, hi[1] * 3)  # lying down: long along the heading
            a = math.radians(dir_deg)
            ca, sa = math.cos(a), math.sin(a)
            corners = []
            for x in xs:
                for y in ys:
                    for z in (0.0, hz):  # pos_asl is the ground point
                        corners.append([pos[0] + x * ca + y * sa, pos[1] - x * sa + y * ca, pos[2] + z])
            px = cam.project(np.array(corners))
            if np.isnan(px).any():
                continue
            x1, y1 = px.min(0)
            x2, y2 = px.max(0)
            if x2 < 0 or y2 < 0 or x1 >= W or y1 >= H:
                continue
            box = [float(np.clip(x1, 0, W)), float(np.clip(y1, 0, H)),
                   float(np.clip(x2, 0, W)), float(np.clip(y2, 0, H))]
            out.append({"tid": int(obj_id), "cls": cls, "grp": grp, "conf": 1.0, "box": [round(v, 1) for v in box]})
        return out
