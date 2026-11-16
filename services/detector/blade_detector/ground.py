"""Box size on the ground from the frame's telemetry: drop boxes no object of their group can fill (model.yaml
max_size_m), and pick the model input size that brings small objects to the size the model knows (auto_imgsz)."""
from __future__ import annotations

import math

import numpy as np

from blade_geometry import Camera

PAD_PX = 3.0  # boxes of tiny objects come out a few px bigger than the object
STRONG_CONF = 0.5  # boxes this sure judge whether the frame's scale is right
MIN_STRONG = 3


def metres_per_px(tel: dict, xyxy: np.ndarray) -> np.ndarray:
    """Metres per tel image pixel at each box: pixel angle x distance to where the box's bottom centre meets flat
    ground through the rangefinder point. NaN without a range or above the horizon."""
    rng = tel.get("range_m") or 0.0
    if rng <= 0 or not len(xyxy):
        return np.full(len(xyxy), np.nan)
    cam = Camera(tel)
    d = cam.rays((xyxy[:, 0] + xyxy[:, 2]) / 2, xyxy[:, 3])
    with np.errstate(divide="ignore", invalid="ignore"):
        t = rng * cam.f[2] / d[:, 2]
    t = np.where(t > 0, np.minimum(t, 2 * rng), np.nan)  # cap: hills break the flat ground far off
    return cam.ifov_rad() * t


def too_big(max_m: np.ndarray, tel: dict, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray, sx: float,
            sy: float) -> np.ndarray:
    """Boxes (decoded image pixels, scaled by sx, sy to tel pixels) whose longer side is longer on the ground than
    max_m[cls]. When most confident boxes of the frame look too big, the telemetry is wrong (another scene, zoom
    lag), not the boxes, so the frame keeps them all."""
    if not len(xyxy) or np.isinf(max_m).all():
        return np.zeros(len(xyxy), bool)
    b = xyxy * np.array([sx, sy, sx, sy])
    side = np.maximum(b[:, 2] - b[:, 0], b[:, 3] - b[:, 1])
    with np.errstate(invalid="ignore"):
        big = side > max_m[cls] / metres_per_px(tel, b) + PAD_PX
    strong = (conf >= STRONG_CONF) & np.isfinite(max_m[cls])
    if strong.sum() >= MIN_STRONG and big[strong].mean() > 0.5:
        return np.zeros(len(xyxy), bool)
    return big


def auto_imgsz(base: int, auto: dict, tel: dict, img_w: int, img_h: int) -> int:
    """Model input size (multiple of 128, base..auto["max"]) at which an object of auto["object_m"] metres at the
    rangefinder point is auto["min_px"] px. base without a range."""
    rng = tel.get("range_m") or 0.0
    if not auto or rng <= 0:
        return base
    px_per_m = img_w / tel["image"]["w"] / (Camera(tel).ifov_rad() * rng)  # decoded image px per metre
    need = max(img_w, img_h) * auto["min_px"] / (auto["object_m"] * px_per_m)
    return int(min(max(base, 128 * math.ceil(need / 128)), auto["max"]))
