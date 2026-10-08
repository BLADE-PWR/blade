"""Frame-number bar: white, black, then nbits squares (LSB first, white = 1)."""
from __future__ import annotations

from typing import NamedTuple

import numpy as np


class Bar(NamedTuple):
    x: int
    y: int
    size: int
    nbits: int

    @property
    def wrap(self) -> int:
        return 1 << self.nbits

    @property
    def width(self) -> int:
        return self.size * (self.nbits + 2)


def decode(img: np.ndarray, bar: Bar) -> int | None:
    """The wrapped frame number in the bar, or None if the sentinels are not there."""
    x, y, s, nb = bar
    if img.shape[0] < y + s or img.shape[1] < x + bar.width:
        return None
    px = img[y + s // 2, x + s // 2:x + bar.width:s]
    lv = px.max(axis=-1) if px.ndim == 2 else px
    bits = lv >= 128
    if not bits[0] or bits[1]:
        return None
    return int(np.sum(bits[2:].astype(np.int64) << np.arange(nb, dtype=np.int64)))


def strip(img: np.ndarray, bar: Bar, margin: int = 2, inplace: bool = False) -> np.ndarray:
    """Black out the bar (plus a margin)."""
    out = img if inplace else img.copy()
    x0, y0 = max(bar.x - margin, 0), max(bar.y - margin, 0)
    out[y0:bar.y + bar.size + margin, x0:bar.x + bar.width + margin] = 0
    return out
