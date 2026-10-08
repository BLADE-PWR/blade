"""Pinhole camera from Arma's tel record: pixels x right / y down, axes right / up / forward."""
from __future__ import annotations

import math

import numpy as np


class Camera:
    def __init__(self, tel: dict):
        c = tel["camera"]
        self.W = float(tel["image"]["w"])
        self.H = float(tel["image"]["h"])
        self.o = np.asarray(c["pos_asl"], dtype=np.float64)
        # re-orthonormalise: the logged axes are rounded
        f = _unit(np.asarray(c["z_axis"], dtype=np.float64))
        r = np.asarray(c["x_axis"], dtype=np.float64)
        r = _unit(r - f * np.dot(r, f))
        u = np.cross(r, f)
        if np.dot(u, c["y_axis"]) < 0:
            u = -u
        self.f, self.r, self.u = f, r, u
        self.tx = math.tan(math.radians(c["hfov_deg"]) / 2)
        self.ty = math.tan(math.radians(c["vfov_deg"]) / 2)
        self.range_m = tel.get("range_m")

    def rays(self, px, py) -> np.ndarray:
        """Pixel coordinates -> unit ray directions (n, 3)."""
        px = np.atleast_1d(np.asarray(px, dtype=np.float64))
        py = np.atleast_1d(np.asarray(py, dtype=np.float64))
        a = (2 * px / self.W - 1) * self.tx
        b = (1 - 2 * py / self.H) * self.ty
        d = self.f[None, :] + a[:, None] * self.r[None, :] + b[:, None] * self.u[None, :]
        return d / np.linalg.norm(d, axis=1, keepdims=True)

    def project(self, pts) -> np.ndarray:
        """World points (n, 3) -> pixels (n, 2); NaN behind the camera."""
        p = np.atleast_2d(np.asarray(pts, dtype=np.float64)) - self.o
        zc = p @ self.f
        out = np.full((len(p), 2), np.nan)
        ok = zc > 0.1
        out[ok, 0] = (0.5 + (p[ok] @ self.r) / (zc[ok] * 2 * self.tx)) * self.W
        out[ok, 1] = (0.5 - (p[ok] @ self.u) / (zc[ok] * 2 * self.ty)) * self.H
        return out

    def ifov_rad(self) -> float:
        """Angle of one pixel at the image centre."""
        return 2 * self.tx / self.W


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)
