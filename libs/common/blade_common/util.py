from __future__ import annotations

import shutil
import zipfile
from pathlib import Path


def inside(poly, x: float, y: float) -> bool:
    """Point in polygon [[x, y], ...] (ray casting)."""
    hit = False
    for (x1, y1), (x2, y2) in zip(poly, [*poly[1:], poly[0]]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1:
            hit = not hit
    return hit


def zip_root(z: zipfile.ZipFile, marker: str) -> str | None:
    """The folder inside the zip that holds `marker` ("" for the top level), or None."""
    hit = next((n for n in z.namelist() if n.endswith(marker)), None)
    return None if hit is None else hit[:-len(marker)]


def extract(z: zipfile.ZipFile, prefix: str, dest: Path) -> None:
    """Extract everything under prefix into dest, skipping paths that would escape it."""
    for n in z.namelist():
        if n.startswith(prefix) and not n.endswith("/") and ".." not in n:
            target = dest / n[len(prefix):]
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
