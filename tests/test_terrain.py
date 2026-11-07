import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from blade_terrain.main import ortho_crop, read_orthos

ROOT = Path(__file__).resolve().parents[1]
WORLD, TILE = 128, 64  # 2 x 2 sat tiles of 64 px over 128 m: 1 m/px


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """A fake grad_meh export (noise, so any seam or offset shows) through terrain/build_ortho.py."""
    d = tmp_path_factory.mktemp("gm")
    full = np.random.default_rng(1).integers(0, 256, (WORLD, WORLD, 3), np.uint8)
    for c in range(2):
        for r in range(2):
            (d / "sat" / str(c)).mkdir(parents=True, exist_ok=True)
            tile = full[r * TILE:(r + 1) * TILE, c * TILE:(c + 1) * TILE]
            rgba = np.dstack([tile, np.full(tile.shape[:2], 255, np.uint8)])
            Image.fromarray(rgba).save(d / "sat" / str(c) / f"{r}.png")
    (d / "meta.json").write_text(json.dumps({"worldName": "t", "worldSize": WORLD}))
    out = d / "pkg"
    subprocess.run([sys.executable, str(ROOT / "terrain" / "build_ortho.py"), str(d), "--out", str(out),
                    "--chunk", "24", "--levels", "3"], check=True, capture_output=True)
    return Image.fromarray(full), read_orthos(out / "ortho")


def test_world_files_put_chunks_in_map_metres(built):
    _, levels = built
    assert list(levels) == [1.0, 2.0, 4.0]
    one = levels[1.0]
    # 64 px tiles in 24 px chunks: 24 + 24 + 16 per tile side, 6 x 6 chunks in all
    assert len(one) == 36
    o = next(o for o in one if o.path.stem == "00064_00024")
    assert (o.left, o.top, o.w, o.h) == (64.0, WORLD - 24.0, 24, 24)
    assert (min(o.left for o in one), max(o.right for o in one)) == (0.0, WORLD)
    assert (min(o.bottom for o in one), max(o.top for o in one)) == (0.0, WORLD)


@pytest.mark.parametrize("x0, y0, x1, y1, w, h", [
    (50, 40, 80, 90, 30, 50),  # 1 m/px, across tile and chunk seams
    (10.3, 70.6, 117.9, 101.2, 150, 43),  # finer than the data, off-grid
])
def test_crop_across_seams_matches_whole_image(built, x0, y0, x1, y1, w, h):
    full, levels = built
    got, mpp = ortho_crop(levels, x0, y0, x1, y1, w, h)
    assert mpp == 1.0
    ref = full.transform((w, h), Image.Transform.EXTENT, (x0, WORLD - y1, x1, WORLD - y0),
                         resample=Image.Resampling.BILINEAR)
    diff = np.abs(np.asarray(got, np.int16) - np.asarray(ref, np.int16))
    assert diff.max() <= 1


def test_crop_picks_level_and_misses_outside(built):
    _, levels = built
    assert ortho_crop(levels, 0, 0, 128, 128, 40, 40)[1] == 2.0  # 3.2 m/px wanted: 2 m level
    assert ortho_crop(levels, 0, 0, 128, 128, 16, 16)[1] == 4.0
    assert ortho_crop(levels, 200, 200, 300, 300, 10, 10) is None
