#!/usr/bin/env python3
"""grad_meh sat/{col}/{row}.png tiles -> ortho/<mpp>m/<px>_<py>.png chunks with .pgw world files,
plus coarser levels (2x, 4x ...)."""
import argparse
import json
import shutil
from pathlib import Path

from PIL import Image

Image.MAX_IMAGE_PIXELS = None  # a grad_meh tile is 7680 x 7680 on Altis

ap = argparse.ArgumentParser()
ap.add_argument("grad_meh_dir", type=Path)
ap.add_argument("--out", type=Path, default=Path("maps/altis"))
ap.add_argument("--chunk", type=int, default=1280)
ap.add_argument("--levels", type=int, default=6)
args = ap.parse_args()

world = float(json.loads((args.grad_meh_dir / "meta.json").read_text())["worldSize"])
tiles = sorted((args.grad_meh_dir / "sat").glob("*/*.png"))
tile = Image.open(tiles[0]).width  # all tiles are the same square size
n_cols = len({p.parent.name for p in tiles})
mpp0 = world / (n_cols * tile)

ortho = args.out / "ortho"
shutil.rmtree(ortho, ignore_errors=True)
for k in range(args.levels):
    (ortho / f"{mpp0 * 2 ** k:g}m").mkdir(parents=True)

for path in tiles:
    col, row = int(path.parent.name), int(path.stem)
    print(path)
    full = Image.open(path).convert("RGB")
    for k in range(args.levels):
        f = 2 ** k
        im = full.reduce(f) if f > 1 else full
        mpp = mpp0 * f
        for y in range(0, im.height, args.chunk):
            for x in range(0, im.width, args.chunk):
                px, py = col * tile // f + x, row * tile // f + y
                stem = ortho / f"{mpp:g}m" / f"{px:05d}_{py:05d}"
                box = (x, y, min(x + args.chunk, im.width), min(y + args.chunk, im.height))
                im.crop(box).save(stem.with_suffix(".png"), compress_level=4)
                left, top = px * mpp + mpp / 2, world - py * mpp - mpp / 2
                stem.with_suffix(".pgw").write_text(f"{mpp:.10g}\n0\n0\n{-mpp:.10g}\n{left:.10g}\n{top:.10g}\n")
