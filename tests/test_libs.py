import json
from pathlib import Path

import numpy as np
import pytest

import frame_code
from blade_common import klv
from blade_common.contracts import schemas
from blade_geometry import Camera, Terrain

ROOT = Path(__file__).resolve().parents[1]


def _render(frame, bar, width=640, height=360, background=90):
    """An image with the bar drawn like the game draws it."""
    x, y, s, nb = bar
    img = np.full((height, width, 3), background, np.uint8)
    for k, white in enumerate([True, False] + [bool((frame >> i) & 1) for i in range(nb)]):
        img[y:y + s, x + s * k:x + s * (k + 1)] = 255 if white else 0
    return img


@pytest.mark.parametrize("bar", [frame_code.Bar(0, 0, 4, 12), frame_code.Bar(3, 5, 4, 12), frame_code.Bar(0, 0, 8, 24)])
def test_bar_roundtrip(bar):
    rng = np.random.default_rng(bar.size)
    wrap = bar.wrap
    for f in [0, 1, 52329 % wrap, wrap - 1, *rng.integers(0, wrap, 30)]:
        img = _render(int(f), bar)
        assert frame_code.decode(img, bar) == int(f)


def test_bar_absent_and_strip():
    bar = frame_code.Bar(0, 0, 4, 12)
    assert frame_code.decode(np.full((360, 640, 3), 90, np.uint8), bar) is None
    img = _render(77, bar)
    assert frame_code.decode(img, bar) == 77
    assert frame_code.decode(frame_code.strip(img, bar), bar) is None


def _height(dem: np.ndarray, x: float, y: float, d: float = 7.5) -> float:
    """Scalar reference for Terrain.height."""
    n = dem.shape[0]
    fx = min(max(x / d, 0.0), n - 1.0)
    fy = min(max(y / d, 0.0), n - 1.0)
    i, j = min(int(fx), n - 2), min(int(fy), n - 2)
    u, v = fx - i, fy - j
    r = n - 1 - j
    sw, se = dem[r, i], dem[r, i + 1]
    nw, ne = dem[r - 1, i], dem[r - 1, i + 1]
    if u + v <= 1:
        return float(sw + u * (se - sw) + v * (nw - sw))
    return float(ne + (1 - u) * (nw - ne) + (1 - v) * (se - ne))


def _dem(n=64, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((n, n)) * 40 + 50).astype(np.float32)


def test_terrain_matches_reference_and_nodes():
    dem = _dem()
    t = Terrain(dem, 7.5)
    rng = np.random.default_rng(1)
    xs, ys = rng.uniform(-20, 64 * 7.5 + 20, 500), rng.uniform(-20, 64 * 7.5 + 20, 500)
    ref = np.array([_height(dem, x, y) for x, y in zip(xs, ys)])
    assert np.allclose(t.height(xs, ys), ref, atol=1e-4)
    # on nodes: exact copy; node (i, j) is dem[n-1-j, i]
    assert t.height(3 * 7.5, 5 * 7.5) == pytest.approx(float(dem[63 - 5, 3]))


def test_terrain_triangle_diagonal():
    dem = np.zeros((2, 2), np.float32)
    dem[0, 1] = 10.0  # NE corner up: the SE-NW diagonal keeps the SW half flat
    t = Terrain(dem, 1.0)
    assert t.height(0.4, 0.4) == pytest.approx(0.0)
    assert t.height(0.8, 0.8) == pytest.approx(6.0)


def test_ray_hits_terrain():
    n = 128
    yy, xx = np.mgrid[0:n, 0:n]
    dem = (60 + 25 * np.sin(xx / 9.0) * np.cos(yy / 13.0)).astype(np.float32)
    t = Terrain(dem, 7.5)
    o = np.array([400.0, 300.0, 400.0])
    targets = np.array([[480.0, 470.0], [300.0, 520.0], [610.0, 610.0]])
    tz = t.height(targets[:, 0], targets[:, 1])
    d = np.c_[targets, tz] - o
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    hit = t.intersect(o, d)
    # the first hit can be earlier than the target if a bump is in the way; here the camera is high
    assert np.allclose(hit[:, :2], targets, atol=0.2)
    sky = t.intersect(o, np.array([[0.0, 0.6, 0.8]]))
    assert np.isnan(sky).all()


def test_camera_project_ray_roundtrip():
    tel = {"camera": {"pos_asl": [19435.35, 11575.718, 320.3],
                      "x_axis": [0.7770736, -0.6294097, 0.0], "y_axis": [0.2182636, 0.2694697, 0.9379483],
                      "z_axis": [0.5903538, 0.7288549, -0.346775], "hfov_deg": 11.655936, "vfov_deg": 6.571955},
           "image": {"w": 1920, "h": 1080}}
    cam = Camera(tel)
    px = np.array([[0, 0], [960, 540], [1919, 1079], [300, 800]], float)
    d = cam.rays(px[:, 0], px[:, 1])
    back = cam.project(cam.o + d * 700)
    assert np.allclose(back, px, atol=1e-6)


def test_klv_roundtrip():
    tel = {"frame": 52329, "camera": {"pos_asl": [1, 2, 3]}, "note": "x" * 300}
    pkt = klv.encode_tel(tel)
    assert pkt[:16] == klv.TEL_KEY
    assert klv.decode_tel(pkt) == tel
    assert klv.decode_tel(klv.encode(klv.PAD_KEY, b"")) is None


def test_committed_schemas_match_models():
    for name, schema in schemas().items():
        f = ROOT / "contracts" / f"{name}.schema.json"
        assert f.exists(), f"run: uv run python -m blade_common.contracts  ({f.name} missing)"
        assert json.loads(f.read_text()) == schema, f"{f.name} is stale: uv run python -m blade_common.contracts"
