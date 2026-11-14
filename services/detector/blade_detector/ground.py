"""Box size on the ground, to drop boxes no object of their group can fill (model.yaml max_width_m)."""
from __future__ import annotations

import numpy as np

from blade_geometry import Camera


def ground_width_m(tel: dict, xyxy: np.ndarray) -> np.ndarray:
    """Width in metres of boxes given in tel image pixels: pixel width x pixel angle x distance to where the box's
    bottom centre meets flat ground through the rangefinder point. NaN without a range or above the horizon."""
    rng = tel.get("range_m") or 0.0
    if rng <= 0 or not len(xyxy):
        return np.full(len(xyxy), np.nan)
    cam = Camera(tel)
    d = cam.rays((xyxy[:, 0] + xyxy[:, 2]) / 2, xyxy[:, 3])
    with np.errstate(divide="ignore", invalid="ignore"):
        t = rng * cam.f[2] / d[:, 2]
    t = np.where(t > 0, np.minimum(t, 2 * rng), np.nan)  # cap: hills break the flat ground far off
    return (xyxy[:, 2] - xyxy[:, 0]) * cam.ifov_rad() * t


def too_wide(max_w: np.ndarray, tel: dict, xyxy: np.ndarray, cls: np.ndarray, sx: float, sy: float) -> np.ndarray:
    """Boxes (decoded image pixels, scaled by sx, sy to tel pixels) wider on the ground than max_w[cls]."""
    if not len(xyxy) or np.isinf(max_w).all():
        return np.zeros(len(xyxy), bool)
    return ground_width_m(tel, xyxy * np.array([sx, sy, sx, sy])) > max_w[cls]
