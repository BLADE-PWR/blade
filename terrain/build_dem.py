#!/usr/bin/env python3
"""grad_meh dem.asc.gz -> map package dem.tif + meta.json, heights unchanged (row 0 = north).
The header's xllcorner / yllcorner are off by half a cell, so node (i, j) is at (i * cell, j * cell)."""
import argparse
import gzip
import json
from pathlib import Path

import numpy as np
import tifffile

ap = argparse.ArgumentParser()
ap.add_argument("grad_meh_dir", type=Path)
ap.add_argument("--out", type=Path, default=Path("maps/altis"))
ap.add_argument("--id", default="altis")
args = ap.parse_args()

with gzip.open(args.grad_meh_dir / "dem.asc.gz", "rt") as f:
    header = {k.lower(): float(v) for k, v in (f.readline().split() for _ in range(6))}
    dem = np.loadtxt(f, dtype=np.float32)
n, cell = dem.shape[0], header["cellsize"]

args.out.mkdir(parents=True, exist_ok=True)
tifffile.imwrite(args.out / "dem.tif", dem)
meta = {"id": args.id, "dem": {"file": "dem.tif", "cell_m": cell, "nodes": n}}
(args.out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
print(f"{args.out / 'dem.tif'}: {n} x {n} nodes, {cell} m, heights {dem.min():.2f} .. {dem.max():.2f} m")
