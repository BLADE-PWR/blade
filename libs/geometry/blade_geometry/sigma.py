"""Expected horizontal error of a ground point from one observation: the anchor's pixel error through
slant range and depression, plus DEM error. SIGMA_BOX comes from tools/pixel_error.py."""
from __future__ import annotations

import math

SIGMA_BOX = {"soldier": 0.07, "tank": 0.10, "military_vehicle": 0.09}  # anchor error / box height
SIGMA_DEM_M = 0.5


def ground_sigma(slant_m: float, depression_rad: float, ifov_rad: float, box_h_px: float, grp: str) -> float:
    ang = SIGMA_BOX.get(grp, 0.1) * box_h_px * ifov_rad
    s = max(math.sin(depression_rad), 0.05)
    t = max(math.tan(depression_rad), 0.05)
    along = math.hypot(slant_m * ang / s, SIGMA_DEM_M / t)
    across = slant_m * ang
    return math.sqrt((along ** 2 + across ** 2) / 2)
