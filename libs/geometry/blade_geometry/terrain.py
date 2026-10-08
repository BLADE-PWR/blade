"""Arma terrain from a DEM (two flat triangles per cell, split along the SE-NW diagonal) and ray-terrain
intersection. dem[0] is the NORTH row; Arma y runs south to north."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import tifffile


class Terrain:
    def __init__(self, dem: np.ndarray, cell_m: float = 7.5, x0: float = 0.0, y0: float = 0.0):
        if dem.ndim != 2 or min(dem.shape) < 2:
            raise ValueError(f"DEM must be a 2-D grid, got {dem.shape}")
        self.dem = np.ascontiguousarray(dem, dtype=np.float32)
        self.d = float(cell_m)
        self.x0, self.y0 = float(x0), float(y0)
        self.ny, self.nx = self.dem.shape
        self.hmin = float(self.dem.min())
        self.hmax = float(self.dem.max())

    @property
    def extent(self) -> tuple[float, float, float, float]:
        """x0, y0, x1, y1 of the node grid."""
        return self.x0, self.y0, self.x0 + (self.nx - 1) * self.d, self.y0 + (self.ny - 1) * self.d

    def height(self, x, y) -> np.ndarray:
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        fx = np.clip((x - self.x0) / self.d, 0.0, self.nx - 1.0)
        fy = np.clip((y - self.y0) / self.d, 0.0, self.ny - 1.0)
        i = np.minimum(fx.astype(np.int64), self.nx - 2)
        j = np.minimum(fy.astype(np.int64), self.ny - 2)
        u, v = fx - i, fy - j
        r = self.ny - 1 - j
        dem = self.dem
        sw, se = dem[r, i], dem[r, i + 1]
        nw, ne = dem[r - 1, i], dem[r - 1, i + 1]
        lower = sw + u * (se - sw) + v * (nw - sw)
        upper = ne + (1 - u) * (nw - ne) + (1 - v) * (se - ne)
        return np.where(u + v <= 1, lower, upper)

    def intersect(self, origin, dirs, tmax: float = 12000.0, tol: float = 0.05) -> np.ndarray:
        """First terrain hit of each ray (march a third of a cell, then bisect); (n, 3), NaN for a miss."""
        o = np.asarray(origin, dtype=np.float64)
        dirs = np.atleast_2d(np.asarray(dirs, dtype=np.float64))
        n = len(dirs)
        out = np.full((n, 3), np.nan)
        if n == 0:
            return out
        dz = dirs[:, 2]
        step = self.d / 3
        # march only between the highest and the lowest terrain height
        t0 = np.where((dz < 0) & (o[2] > self.hmax), (o[2] - self.hmax) / np.maximum(-dz, 1e-9) - step, 0.0)
        t0 = np.maximum(t0, 0.0)
        t1 = np.where(dz < 0, (o[2] - self.hmin) / np.maximum(-dz, 1e-9) + step, tmax)
        t1 = np.minimum(t1, tmax)
        live = ~((dz >= 0) & (o[2] > self.hmax)) & (t1 > t0)
        if not live.any():
            return out
        m = int(np.ceil((t1[live] - t0[live]).max() / step)) + 2
        k = np.arange(m, dtype=np.float64)
        t = t0[:, None] + k[None, :] * step
        p = o[None, None, :] + t[..., None] * dirs[:, None, :]
        below = (p[..., 2] - self.height(p[..., 0], p[..., 1]) <= 0) & (t <= t1[:, None]) & live[:, None]
        hit = below.any(axis=1)
        if not hit.any():
            return out
        idx = below.argmax(axis=1)
        rows = np.nonzero(hit)[0]
        hi = t[rows, idx[rows]]
        lo = np.where(idx[rows] > 0, hi - step, hi)
        d_ = dirs[rows]
        for _ in range(int(np.ceil(np.log2(step / tol))) + 1):
            mid = (lo + hi) / 2
            q = o + mid[:, None] * d_
            above = q[:, 2] - self.height(q[:, 0], q[:, 1]) > 0
            lo = np.where(above, mid, lo)
            hi = np.where(above, hi, mid)
        q = o + ((lo + hi) / 2)[:, None] * d_
        out[rows, 0], out[rows, 1] = q[:, 0], q[:, 1]
        out[rows, 2] = self.height(q[:, 0], q[:, 1])
        return out


@dataclass
class MapPackage:
    """maps/<id>/: meta.json + dem.tif (float32, row 0 = north)."""
    id: str
    root: Path
    meta: dict
    terrain: Terrain

    @classmethod
    def load(cls, root: str | Path) -> MapPackage:
        root = Path(root)
        meta = json.loads((root / "meta.json").read_text())
        dm = meta.get("dem", {})
        f = root / dm.get("file", "dem.tif")
        dem = np.asarray(tifffile.imread(f), dtype=np.float32)
        nodes = dm.get("nodes")
        if nodes and dem.shape != (nodes, nodes):
            raise ValueError(f"{f}: shape {dem.shape}, meta.json says {nodes} x {nodes} nodes")
        origin = dm.get("origin_m", [0.0, 0.0])
        return cls(id=meta.get("id", root.name), root=root, meta=meta,
                   terrain=Terrain(dem, dm.get("cell_m", 7.5), origin[0], origin[1]))
