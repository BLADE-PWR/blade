import json
import time
import types

import numpy as np
import pytest
from conftest import frame_image, look_at_tel, write_map

from blade_common.contracts import Envelope
from blade_detector.oracle import Oracle
from blade_geometry import Camera, MapPackage
from blade_georef.project import project_frame
from blade_ingest.main import frame_ts
from blade_ingest.source import Source
from blade_sessions.recording import finalize, load_index
from blade_uplink.encoder import TsWriter
from blade_world.main import World


@pytest.fixture(scope="module")
def map_dir(tmp_path_factory):
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]
    return write_map(tmp_path_factory.mktemp("maps") / "tiny", 80 + 20 * np.sin(xx / 15.0) * np.cos(yy / 21.0))


@pytest.fixture(scope="module")
def pkg(map_dir):
    return MapPackage.load(map_dir)


CENTRE = (960.0, 960.0)


class BytesSink:
    def __init__(self):
        self.data = bytearray()

    def write(self, b):
        self.data += bytes(b)
        return len(b)

    def flush(self):
        pass

    def close(self):
        pass


@pytest.fixture(scope="module")
def ts_bytes(pkg):
    sink = BytesSink()
    w = TsWriter(sink, "t-uav1", 640, 360, fps=30, bitrate="2M", encoder="libx264")
    t0 = time.time()
    for k in range(45):
        t = k / 30
        tel = dict(look_at_tel(pkg.terrain, CENTRE, 1000 + k, bearing_deg=k), sid="t-uav1", t_capture=t0 + t)
        w.write(frame_image(k), tel, t0 + t)
    w.close()
    return bytes(sink.data)


def test_ingest_pairs_every_frame(ts_bytes):
    got = []
    src = Source(key="test", kind="replay", jpeg_w=320, jpeg_h=180, on_frame=got.append)
    for i in range(0, len(ts_bytes), 1316):
        src.feed(ts_bytes[i:i + 1316], block=True)
    src.close()
    assert src.done.wait(20)
    frames = [p.tel["frame"] for p in got]
    assert frames == list(range(1000, 1045))
    assert got[0].sid == "t-uav1" and got[0].jpeg[:2] == b"\xff\xd8"
    assert src.unpaired == 0


def test_replay_frames_carry_their_recording_time():
    tel = {"frame": 7, "t_capture": 1000.25}
    replay = frame_ts("replay", tel, 5000.0, 5000.01)
    assert replay == {"capture": 5000.0, "ingest": [5000.0, 5000.01], "media": 1000.25}  # latency from the arrival
    live = frame_ts("live", tel, 5000.0, 5000.01)
    assert live["capture"] == 1000.25 and "media" not in live  # live streams stay on the wall clock
    assert frame_ts("replay", {"frame": 7}, 5000.0, 5000.01)["media"] == 5000.0  # stills: their arrival


def test_recording_scan_builds_index(tmp_path, ts_bytes):
    (tmp_path / "stream.ts").write_bytes(ts_bytes)
    m = finalize(tmp_path)
    assert m["frames"] == 45 and m["frame_first"] == 1000 and m["frame_last"] == 1044
    assert m["sid"] == "t-uav1" and m["image"] == {"w": 640, "h": 360}
    idx = load_index(tmp_path)
    assert sum(e["n"] for e in idx) == len(ts_bytes)
    assert idx[-1]["t"] == pytest.approx(44 / 30, abs=0.01)


def test_georef_hits_known_point(pkg):
    tel = look_at_tel(pkg.terrain, CENTRE, 1, bearing_deg=30)
    cam = Camera(tel)
    x, y = CENTRE[0] + 25.0, CENTRE[1] - 40.0
    target = np.array([x, y, float(pkg.terrain.height(x, y))])
    u, v = cam.project(target[None])[0]
    # soldier: bottom-centre on the terrain; tank: box centre at half its height above the terrain
    uc, vc = cam.project((target + [0, 0, 1.3])[None])[0]
    geo = project_frame(pkg.terrain, tel, [
        {"tid": 1, "cls": "soldier_standing", "grp": "soldier", "conf": 0.9, "box": [u - 5, v - 10, u + 5, v]},
        {"tid": 2, "cls": "tank", "grp": "tank", "conf": 0.9, "box": [uc - 20, vc - 8, uc + 20, vc + 8]}])
    for p in geo["pts"]:
        assert np.allclose(p["xyz"][:2], target[:2], atol=0.1)
    assert geo["range_check_m"] < 0.5 and all(geo["footprint"])


def test_oracle_box_contains_object(pkg):
    o = Oracle()
    objs = []  # (id, cls, stance, ground point)
    for k, (dx, dy) in enumerate([(0, 0), (-30, 20), (40, -10), (15, 35)]):
        x, y = CENTRE[0] + dx, CENTRE[1] + dy
        cls, stance = ("tank", "") if k == 3 else ("soldier", ["STAND", "CROUCH", "PRONE"][k % 3])
        objs.append((k + 1, cls, stance, [x, y, float(pkg.terrain.height(x, y))]))
        half = [1.8, 3.5, 1.3] if cls == "tank" else [0.3, 0.3, 0.9]
        o.on_spawn("s", {"id": k + 1, "cls": cls, "bbox_min": [-v for v in half], "bbox_max": half})
    o.on_gt("s", {"frame": 5, "objs": [[i, p, 30.0, [0, 0, 0], st] for i, _, st, p in objs]})
    tel = look_at_tel(pkg.terrain, CENTRE, 6)
    dets = o.detect("s", tel)
    assert len(dets) == len(objs)
    assert {d["grp"] for d in dets} == {"soldier", "tank"}
    cam = Camera(tel)
    by_id = {i: p for i, _, _, p in objs}
    for d in dets:
        u, v = cam.project(np.array([by_id[d["tid"]]]))[0]
        x1, y1, x2, y2 = d["box"]
        assert x1 - 1 <= u <= x2 + 1 and y1 - 1 <= v <= y2 + 1


def test_world_passthrough_and_expiry(monkeypatch):
    w = World("w", confirm_n=1)
    pts = [{"tid": 3, "cls": "soldier_prone", "grp": "soldier", "conf": 0.8, "xyz": [10.0, 20.0, 5.0], "sigma_m": 2.0},
           {"tid": None, "cls": "tank", "grp": "tank", "conf": 0.3, "xyz": [1.0, 1.0, 1.0]},
           {"tid": 9, "cls": "explosion", "grp": "explosion", "conf": 0.7, "xyz": [5.0, 5.0, 1.0]}]
    changed, events = w.observe("s1", pts, 100.0)
    assert [o.oid for o in changed] == ["w-0001"] and changed[0].model().pose == "prone"
    assert events[0]["kind"] == "explosion"
    changed, _ = w.observe("s1", [dict(pts[0], xyz=[11.0, 21.0, 5.0])], 101.0)
    assert changed[0].oid == "w-0001" and changed[0].n == 2
    stale, expired = w.sweep(105.0)
    assert stale[0].state == "stale" and not expired
    _, expired = w.sweep(200.0)
    assert not expired and w.objects["w-0001"].state == "stale"  # remembered for re-acquisition
    _, expired = w.sweep(101.0 + 601.0)
    assert expired == ["w-0001"] and not w.objects


def test_stills_import(tmp_path, pkg):
    from PIL import Image

    from blade_sessions.recording import load_truth
    from blade_sessions.stills import main as import_stills

    ds = tmp_path / "dataset"
    (ds / "meta").mkdir(parents=True)
    (ds / "images" / "val").mkdir(parents=True)
    for k in range(3):
        tel = look_at_tel(pkg.terrain, CENTRE, 500 + k, bearing_deg=40 * k)
        img = frame_image(k)
        Image.fromarray(img).save(ds / "images" / "val" / f"p{k}.png")
        c = tel["camera"]
        (ds / "meta" / f"p{k}.json").write_text(json.dumps({
            "image": f"images/val/p{k}.png", "width": 640, "height": 360, "session": "s1", "frame": 500 + k,
            "camera": {"pos_asl": c["pos_asl"], "right_axis": c["x_axis"], "up_axis": c["y_axis"],
                       "forward_axis": c["z_axis"], "hfov_deg": c["hfov_deg"], "vfov_deg": c["vfov_deg"]},
            "laser_rangefinder": {"range": tel["range_m"]},
            "objects": [{"type": "soldier", "object_id": 3, "stance": "prone", "ground_pos_asl": [1.0, 2.0, 3.0]}]}))
    out = tmp_path / "rec"
    assert import_stills([str(ds), "--out", str(out), "--split", "val"]) == 0
    m = json.loads((out / "manifest.json").read_text())
    assert m["frames"] == 3 and m["source"] == "stills" and m["image"] == {"w": 640, "h": 360}
    kinds = [r["kind"] for r in load_truth(out)]
    assert kinds == ["spawn", "gt", "gt", "gt"]


def test_size_filter_drops_boxes_too_big_on_the_ground():
    from blade_detector.ground import metres_per_px, too_big

    flat = types.SimpleNamespace(height=lambda x, y: 0.0)
    tel = look_at_tel(flat, (0.0, 0.0), 0)
    px_per_m = 1 / (Camera(tel).ifov_rad() * tel["range_m"])
    box = np.array([[320 - 2 * px_per_m, 170, 320 + 2 * px_per_m, 180]])  # 4 m wide, bottom on the aim point
    assert 1 / metres_per_px(tel, box)[0] == pytest.approx(px_per_m, rel=0.02)
    tall = np.array([[318, 180 - 4 * px_per_m, 322, 180]])  # 4 m tall, narrow
    max_m = np.array([3.0, 20.0])
    two = np.repeat(box, 2, 0), np.array([0.9, 0.9]), np.array([0, 1])
    assert too_big(max_m, tel, two[0], *two[1:], 1.0, 1.0).tolist() == [True, False]
    assert too_big(max_m, tel, tall, np.array([0.9]), np.array([0]), 1.0, 1.0).tolist() == [True]
    assert not too_big(max_m, dict(tel, range_m=None), box, np.array([0.9]), np.array([0]), 1.0, 1.0).any()
    # most confident boxes too big: the telemetry is wrong, not the boxes
    many = np.repeat(box, 4, 0), np.full(4, 0.9), np.zeros(4, int)
    assert not too_big(max_m, tel, *many, 1.0, 1.0).any()


def test_auto_imgsz_brings_small_objects_to_min_px():
    from blade_detector.ground import auto_imgsz

    flat = types.SimpleNamespace(height=lambda x, y: 0.0)
    tel = look_at_tel(flat, (0.0, 0.0), 0, w=1280, h=720, hfov=34.0, dist_m=300.0, alt_m=120.0)
    auto = {"max": 2304, "object_m": 7.0, "min_px": 60}
    px_per_m = 1 / (Camera(tel).ifov_rad() * tel["range_m"])
    size = auto_imgsz(1280, auto, tel, 1280, 720)
    assert size % 128 == 0 and 7 * px_per_m * size / 1280 >= 60 > 7 * px_per_m * (size - 128) / 1280
    assert auto_imgsz(1280, dict(auto, max=1536), tel, 1280, 720) == 1536
    assert auto_imgsz(1280, auto, dict(tel, camera=dict(tel["camera"], hfov_deg=5.0)), 1280, 720) == 1280
    assert auto_imgsz(1536, {}, tel, 1280, 720) == 1536


def test_rendezvous_is_stable():
    from blade_detector.cluster import rendezvous

    sids = [f"s{k}" for k in range(40)]
    two = {s: rendezvous(s, ["a", "b"]) for s in sids}
    three = {s: rendezvous(s, ["a", "b", "c"]) for s in sids}
    assert set(two.values()) == {"a", "b"}
    moved = [s for s in sids if two[s] != three[s]]
    assert all(three[s] == "c" for s in moved)  # adding a replica only takes streams, never shuffles


class _Msg:
    def __init__(self, subject, data, t):
        self.subject, self.data, self.headers = subject, data, {"t": str(t)}


async def test_recorder_takes_no_new_recording_while_closing(tmp_path, ts_bytes):
    from blade_sessions.recorder import Recorder

    rec = Recorder(tmp_path, record_all=True)
    await rec.on_raw(_Msg("raw.s1", ts_bytes, 1.0))
    assert list(rec.active) == ["s1"]
    await rec.close()  # finalizes s1
    await rec.on_raw(_Msg("raw.s1", ts_bytes[-4096:], 2.0))  # ingest's tail arrives after the close
    assert rec.active == {}
    dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert len(dirs) == 1 and not json.loads((dirs[0] / "manifest.json").read_text())["recording"]


def test_recover_removes_scraps_and_finalizes_real_streams(tmp_path, ts_bytes):
    from blade_sessions.recorder import Recorder

    big = ts_bytes * (1 + (1 << 20) // len(ts_bytes))  # over the 1 MB scrap limit, still valid TS
    for name, data in (("scrap", ts_bytes[:4096]), ("crashed", big), ("done", ts_bytes)):
        d = tmp_path / name
        d.mkdir()
        (d / "stream.ts").write_bytes(data)
        (d / "manifest.json").write_text(json.dumps({"id": name, "recording": name != "done"}))
    assert sorted(Recorder(tmp_path, record_all=True).recover()) == ["crashed", "scrap"]
    assert not (tmp_path / "scrap").exists()
    m = json.loads((tmp_path / "crashed" / "manifest.json").read_text())
    assert m["recording"] is False and m["recovered"] and m["frames"] > 0
    assert json.loads((tmp_path / "done" / "manifest.json").read_text()) == {"id": "done", "recording": False}


class FakeNats:
    def __init__(self):
        self.sent = []

    async def publish(self, subject, data, headers=None):
        self.sent.append((subject, json.loads(data)))


class GeoMsg:
    def __init__(self, env):
        self.data, self.headers = env.model_dump_json().encode(), None


def world_service(monkeypatch):
    from blade_world import main as wm
    nc = FakeNats()
    for name, value in (("state", {"nc": nc}), ("worlds", {}), ("clocks", {}), ("swept", {})):
        monkeypatch.setattr(wm, name, value)
    return wm, nc


def tank_geo(k, media=None):
    ts = {"capture": time.time()} | ({"media": media} if media is not None else {})
    pts = [{"tid": 1, "cls": "tank", "grp": "tank", "conf": 0.9, "xyz": [500.0, 500.0, 5.0], "sigma_m": 1.0}]
    return GeoMsg(Envelope(sid="rp", wid="w1", frame=k, ts=ts, data={"pts": pts}))


async def test_paused_replay_does_not_age_the_world(monkeypatch):
    wm, nc = world_service(monkeypatch)
    for k in range(30):
        await wm.on_geo(tank_geo(k, media=500 + k / 10))
    t_end = nc.sent[-1][1]["data"]["t"]
    await wm.sweep_once(time.time() + 120)  # paused for two minutes
    _, beat = nc.sent[-1]
    assert beat["data"]["t"] == t_end and not beat["data"].get("objects")  # nothing went stale
    sent = len(nc.sent)
    await wm.sweep_once(time.time() + 240)
    assert len(nc.sent) == sent  # the clock stands still: nothing to publish


async def test_live_world_ages_and_beats(monkeypatch):
    wm, nc = world_service(monkeypatch)
    for k in range(30):
        await wm.on_geo(tank_geo(k))
    await wm.sweep_once(time.time() + 4)  # the drone stopped sending
    _, beat = nc.sent[-1]
    assert [o["state"] for o in beat["data"]["objects"]] == ["stale"] and beat["data"]["t"] > time.time() + 3
