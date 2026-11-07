"""Test helpers: a tiny map package, a camera looking at a point, and frames to encode."""
import json
import math
from pathlib import Path

import numpy as np
import tifffile


def write_map(d: Path, dem: np.ndarray, cell: float = 7.5) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(d / "dem.tif", dem.astype(np.float32))
    (d / "meta.json").write_text(json.dumps({"id": d.name, "dem": {"file": "dem.tif", "cell_m": cell,
                                                                   "nodes": dem.shape[0]}}))
    return d


def look_at_tel(terrain, target_xy, frame: int, w: int = 640, h: int = 360, hfov: float = 14.0,
                bearing_deg: float = 0.0, dist_m: float = 400.0, alt_m: float = 300.0) -> dict:
    """A tel record for a camera dist_m away (from bearing_deg) and alt_m above target_xy, aimed at it."""
    tz = float(terrain.height(*target_xy))
    target = np.array([target_xy[0], target_xy[1], tz])
    a = math.radians(bearing_deg)
    pos = target + [dist_m * math.sin(a), -dist_m * math.cos(a), alt_m]
    f = (target - pos) / np.linalg.norm(target - pos)
    r = np.cross(f, [0.0, 0.0, 1.0])
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    vfov = math.degrees(2 * math.atan(math.tan(math.radians(hfov) / 2) * h / w))
    return {"frame": frame, "camera": {"pos_asl": pos.tolist(), "x_axis": r.tolist(), "y_axis": u.tolist(),
                                       "z_axis": f.tolist(), "hfov_deg": hfov, "vfov_deg": vfov},
            "range_m": float(np.linalg.norm(target - pos)), "image": {"w": w, "h": h}}


def frame_image(k: int, w: int = 640, h: int = 360) -> np.ndarray:
    """A BGR frame with some texture that moves with k."""
    yy, xx = np.mgrid[0:h, 0:w]
    v = 128 + 60 * np.sin((xx + 4 * k) / 23.0) * np.cos(yy / 17.0)
    return np.repeat(v[..., None], 3, axis=2).astype(np.uint8)
