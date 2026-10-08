"""Haze correction, shared by the dataset builder and the detector."""
from __future__ import annotations

import math

TARGET = 0.48  # median luma after correction
SAT = 1.05  # saturation boost
LO_PCT, HI_PCT = 0.3, 99.8  # haze floor / white point percentiles
MAX_GAIN = 2.5  # never stretch more than this
WB = 0.25  # share of per-channel tint removal


def enhance(im):
    """PIL image -> corrected PIL RGB image (same size)."""
    import numpy as np
    from PIL import Image

    a = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    small = a[::8, ::8].reshape(-1, 3)
    lo_c = np.percentile(small, LO_PCT, axis=0)
    hi_c = np.percentile(small, HI_PCT, axis=0)
    lo = lo_c.min() + WB * (lo_c - lo_c.min())  # mostly one common stretch ...
    hi = hi_c.max() + WB * (hi_c - hi_c.max())  # ... + a little tint removal
    span = np.maximum(hi - lo, 1.0 / MAX_GAIN)
    lo = np.minimum(lo, hi - span)
    out = np.clip((a - lo) / span, 0.0, 1.0)
    w = np.array([0.299, 0.587, 0.114], np.float32)
    m = float(np.median(out[::8, ::8] @ w))
    g = min(max(math.log(TARGET) / math.log(min(max(m, 0.05), 0.95)), 0.7), 2.5)
    out = out ** g
    luma = (out @ w)[..., None]
    out = np.clip(luma + (out - luma) * SAT, 0.0, 1.0)
    return Image.fromarray((out * 255.0 + 0.5).astype(np.uint8))
