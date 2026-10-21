"""terrain: map packages (maps/<id>/meta.json, dem.tif, ortho/). GET /maps/{id}/crop?x0=&y0=&x1=&y1=&mpp=&layer=
returns a north-up PNG; ortho chunks with the same m/px form a level, and auto falls back to the DEM hillshade."""
from __future__ import annotations

import io
import logging
import math
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from fastapi import FastAPI, HTTPException, Response
from PIL import Image

from blade_common import ports
from blade_common.service import env_str, run_app
from blade_geometry import MapPackage

log = logging.getLogger("terrain")
MAPS_DIR = Path(env_str("MAPS_DIR", "maps"))
MAX_PX = 2048
Image.MAX_IMAGE_PIXELS = None  # whole-map orthophotos are large


@dataclass(frozen=True)
class Ortho:
    """One north-up image; left/top are its outer edges in map metres."""
    path: Path
    mpp: float
    left: float
    top: float
    w: int
    h: int

    @property
    def right(self) -> float:
        return self.left + self.w * self.mpp

    @property
    def bottom(self) -> float:
        return self.top - self.h * self.mpp

    def touches(self, x0: float, y0: float, x1: float, y1: float) -> bool:
        return self.left < x1 and self.right > x0 and self.bottom < y1 and self.top > y0


def read_orthos(d: Path) -> dict[float, list[Ortho]]:
    """Every <png> + <pgw> under d, grouped by m/px. Rotated or non-square pixels are skipped."""
    levels: dict[float, list[Ortho]] = {}
    for png in sorted(d.rglob("*.png")):
        wf = png.with_suffix(".pgw")
        if not wf.exists():
            continue
        A, D, B, E, C, F = (float(v) for v in wf.read_text().split()[:6])
        if D or B or A <= 0 or abs(A + E) > 1e-9 * A:
            log.warning("%s: not north-up with square pixels, skipped", wf)
            continue
        with Image.open(png) as im:
            w, h = im.size
        levels.setdefault(round(A, 9), []).append(Ortho(png, A, C - A / 2, F + A / 2, w, h))  # .pgw: pixel centres
    return dict(sorted(levels.items()))


@lru_cache(maxsize=32)
def chunk_rgb(path: Path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def ortho_crop(levels: dict[float, list[Ortho]], x0: float, y0: float, x1: float, y1: float, w: int, h: int,
               fill=(40, 40, 40)) -> tuple[Image.Image, float] | None:
    """w x h picture of [x0, x1] x [y0, y1] from the best level, or None if no image touches the area."""
    usable = {m: hit for m, os_ in levels.items() if (hit := [o for o in os_ if o.touches(x0, y0, x1, y1)])}
    if not usable:
        return None
    want = (x1 - x0) / w
    finer = [m for m in usable if m <= want * (1 + 1e-6)]
    m = max(finer) if finer else min(usable)
    chunks, mpp = usable[m], usable[m][0].mpp
    # a canvas on the level's pixel grid, one pixel wider on each side for bilinear sampling
    ox, oy = chunks[0].left, chunks[0].top
    c0, c1 = math.floor((x0 - ox) / mpp) - 1, math.ceil((x1 - ox) / mpp) + 1
    r0, r1 = math.floor((oy - y1) / mpp) - 1, math.ceil((oy - y0) / mpp) + 1
    canvas = Image.new("RGB", (c1 - c0, r1 - r0), fill)
    for o in levels[m]:
        cx, cy = round((o.left - ox) / mpp), round((oy - o.top) / mpp)
        a0, a1, b0, b1 = max(c0, cx), min(c1, cx + o.w), max(r0, cy), min(r1, cy + o.h)
        if a1 > a0 and b1 > b0:
            canvas.paste(chunk_rgb(o.path).crop((a0 - cx, b0 - cy, a1 - cx, b1 - cy)), (a0 - c0, b0 - r0))
    box = ((x0 - ox) / mpp - c0, (oy - y1) / mpp - r0, (x1 - ox) / mpp - c0, (oy - y0) / mpp - r0)
    out = canvas.transform((w, h), Image.Transform.EXTENT, box, resample=Image.Resampling.BILINEAR, fillcolor=fill)
    return out, mpp


@lru_cache(maxsize=4)
def package(mid: str) -> MapPackage:
    d = MAPS_DIR / mid
    if "/" in mid or mid.startswith(".") or not (d / "meta.json").exists():
        raise HTTPException(404, f"no map {mid}")
    return MapPackage.load(d)


def hillshade(dem: np.ndarray, cell: float) -> np.ndarray:
    """DEM (row 0 = north) -> coloured RGB uint8 relief, lit from the north-west."""
    gy, gx = np.gradient(dem.astype(np.float32), cell)
    light = np.array([-0.5, 0.5, 0.7]) / np.linalg.norm([-0.5, 0.5, 0.7])
    shade = np.clip((-gx * light[0] + gy * light[1] + light[2]) / np.sqrt(gx ** 2 + gy ** 2 + 1), 0, 1)
    h = (dem - dem.min()) / max(float(np.ptp(dem)), 1e-6)
    rgb = np.stack([0.35 + 0.35 * h, 0.45 + 0.25 * h, 0.25 + 0.15 * h], -1) * (0.25 + 0.75 * shade[..., None])
    rgb[dem < 0] = (0.18, 0.32, 0.5)  # sea
    return (np.clip(rgb, 0, 1) * 255).astype(np.uint8)


@lru_cache(maxsize=4)
def shade(mid: str) -> Image.Image:
    t = package(mid).terrain
    return Image.fromarray(hillshade(t.dem, t.d))


@lru_cache(maxsize=4)
def orthos(mid: str) -> dict[float, list[Ortho]]:
    return read_orthos(MAPS_DIR / mid / "ortho")


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("maps in %s: %s", MAPS_DIR, [m["id"] for m in list_maps()])
    yield


app = FastAPI(title="blade terrain", lifespan=lifespan)


def list_maps() -> list[dict]:
    return [{"id": d.parent.name, "path": str(d.parent)} for d in sorted(MAPS_DIR.glob("*/meta.json"))]


@app.get("/health")
async def health():
    return {"ok": True, "maps": [m["id"] for m in list_maps()]}


@app.get("/maps")
async def maps():
    return list_maps()


@app.get("/maps/{mid}/meta")
async def meta(mid: str):
    p = package(mid)
    x0, y0, x1, y1 = p.terrain.extent
    return {**p.meta, "extent": [x0, y0, x1, y1], "heights": [p.terrain.hmin, p.terrain.hmax],
            "ortho": [{"mpp": m, "images": len(os_),
                       "extent": [min(o.left for o in os_), min(o.bottom for o in os_),
                                  max(o.right for o in os_), max(o.top for o in os_)]}
                      for m, os_ in orthos(mid).items()] or p.meta.get("ortho")}


@app.get("/maps/{mid}/height")
async def height(mid: str, x: float, y: float):
    return {"x": x, "y": y, "h": float(package(mid).terrain.height(x, y))}


@app.get("/maps/{mid}/crop")
async def crop(mid: str, x0: float, y0: float, x1: float, y1: float, mpp: float = 1.0, layer: str = "auto"):
    if x1 <= x0 or y1 <= y0 or mpp <= 0:
        raise HTTPException(400, "need x0 < x1, y0 < y1, mpp > 0")
    w, h = int(round((x1 - x0) / mpp)), int(round((y1 - y0) / mpp))
    if w > MAX_PX or h > MAX_PX:
        k = max(w, h) / MAX_PX
        w, h = int(w / k), int(h / k)
    w, h = max(w, 1), max(h, 1)
    got = ortho_crop(orthos(mid), x0, y0, x1, y1, w, h) if layer in ("auto", "ortho") else None
    if got is None and layer == "ortho":
        raise HTTPException(404, "no orthophoto covers this area")
    if got is not None:
        out, used = got[0], f"ortho {got[1]:g} m/px"
    else:
        t = package(mid).terrain
        # node (i, j) is pixel (i, ny-1-j); pixel centres are at +0.5 in PIL's edge coordinates
        box = ((x0 - t.x0) / t.d + 0.5, (t.ny - 1) - (y1 - t.y0) / t.d + 0.5,
               (x1 - t.x0) / t.d + 0.5, (t.ny - 1) - (y0 - t.y0) / t.d + 0.5)
        out = shade(mid).transform((w, h), Image.Transform.EXTENT, box, resample=Image.Resampling.BILINEAR,
                                   fillcolor=(40, 40, 40))
        used = "hillshade"
    buf = io.BytesIO()
    out.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png",
                    headers={"X-Layer": used, "X-Extent": f"{x0},{y0},{x1},{y1}", "Cache-Control": "max-age=60"})


def main() -> None:
    run_app("blade_terrain.main:app", ports.TERRAIN)
