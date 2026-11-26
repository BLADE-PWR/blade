import asyncio
import types

import pytest
from conftest import look_at_tel

from blade_detector.routing import Entry, Router, Routing, describe, scale_range

FLAT = types.SimpleNamespace(height=lambda x, y: 0.0)
TEL = look_at_tel(FLAT, (0.0, 0.0), 0, w=1280, h=720, hfov=34.0, dist_m=300.0, alt_m=120.0)


def pkg(min_px=None, max_px=None, imgsz=1280, scale=True):
    return types.SimpleNamespace(scale={"object_m": 1.8, "min_px": min_px, "max_px": max_px} if scale else {},
                                 imgsz_max=imgsz)


def tel_at(px: float) -> dict:
    """The telemetry on which a 1.8 m object takes px pixels at a 1280 model input."""
    from blade_detector.ground import px_on_target

    base = px_on_target(TEL, 1.8, 1280, 1280, 720)
    return dict(TEL, range_m=TEL["range_m"] * base / px)


def choose(router, routing, packages, px, t, sid="s"):
    return router.choose(routing, packages, sid, tel_at(px), 1280, 720, t)


def test_first_model_whose_range_holds_the_frame_runs_it():
    packages = {"near@1": pkg(min_px=14), "far@1": pkg(min_px=6, max_px=14)}
    routing = Routing.model_validate({"models": [{"model": "near@1"}, {"model": "far@1"}]})
    assert choose(Router(), routing, packages, 30, 0).model == "near@1"
    assert choose(Router(), routing, packages, 9, 0).model == "far@1"
    assert choose(Router(), routing, packages, 3, 0).model is None


def test_the_running_model_keeps_the_stream_at_the_edge_of_its_range():
    packages = {"near@1": pkg(min_px=14)}
    routing = Routing.model_validate({"models": [{"model": "near@1"}]})
    router = Router()
    assert choose(router, routing, packages, 20, 0).model == "near@1"
    assert choose(router, routing, packages, 13, 1).model == "near@1"  # inside the 15 % margin: 11.9
    assert choose(router, routing, packages, 15, 2).model == "near@1"
    assert choose(router, routing, packages, 13.5, 3).model == "near@1"


def test_a_model_changes_only_after_the_frame_stays_outside_for_hold_s():
    packages = {"near@1": pkg(min_px=14), "far@1": pkg(min_px=6, max_px=14)}
    routing = Routing.model_validate({"models": [{"model": "near@1"}, {"model": "far@1"}], "hold_s": 1.0})
    router = Router()
    assert choose(router, routing, packages, 30, 0).model == "near@1"
    assert choose(router, routing, packages, 8, 1).model == "near@1"  # out of range, waiting
    assert choose(router, routing, packages, 8, 1.5).model == "near@1"
    assert choose(router, routing, packages, 8, 2.1).model == "far@1"
    assert choose(router, routing, packages, 3, 3).model == "far@1"  # a single wait restarts per target
    assert choose(router, routing, packages, 3, 4.5).model is None
    back = choose(router, routing, packages, 30, 5)  # in range again, but none stays for hold_s
    assert back.model is None and back.px == pytest.approx(30)
    assert choose(router, routing, packages, 30, 6.1).model == "near@1"


def test_a_flicker_across_the_edge_does_not_switch():
    packages = {"near@1": pkg(min_px=14), "far@1": pkg(min_px=6, max_px=14)}
    routing = Routing.model_validate({"models": [{"model": "near@1"}, {"model": "far@1"}], "hold_s": 1.0})
    router = Router()
    choose(router, routing, packages, 30, 0)
    assert choose(router, routing, packages, 8, 1).model == "near@1"
    assert choose(router, routing, packages, 30, 1.8).model == "near@1"  # back in range: the wait is over
    assert choose(router, routing, packages, 8, 2.0).model == "near@1"
    assert choose(router, routing, packages, 8, 2.9).model == "near@1"  # waits from 2.0, not from 1.0


def test_without_a_range_the_last_choice_stays():
    packages = {"near@1": pkg(min_px=14), "far@1": pkg(min_px=6, max_px=14)}
    routing = Routing.model_validate({"models": [{"model": "near@1"}, {"model": "far@1"}]})
    router, blind = Router(), dict(TEL, range_m=None)
    assert router.choose(routing, packages, "s", blind, 1280, 720, 0).model == "near@1"  # start: the first model
    choose(router, routing, packages, 9, 1)
    choose(router, routing, packages, 9, 3)
    assert router.choose(routing, packages, "s", blind, 1280, 720, 4).model == "far@1"


def test_streams_are_chosen_apart_and_forgotten():
    packages = {"near@1": pkg(min_px=14)}
    routing = Routing.model_validate({"models": [{"model": "near@1"}]})
    router = Router()
    assert choose(router, routing, packages, 30, 0, "a").model == "near@1"
    assert choose(router, routing, packages, 3, 0, "b").model is None
    router.forget("a")
    assert router.current("a") is None and router.current("b").model is None


def test_an_override_replaces_model_yaml_and_a_model_not_loaded_is_skipped():
    packages = {"near@1": pkg(min_px=14)}
    routing = Routing.model_validate({"models": [{"model": "gone@1"}, {"model": "near@1", "min_px": 4}]})
    assert choose(Router(), routing, packages, 5, 0).model == "near@1"
    assert scale_range(packages["near@1"], routing.models[1]) == (1.8, 4.0, float("inf"))
    d = describe(routing, packages)["models"]
    assert d[0] == {"model": "gone@1", "loaded": False}
    assert d[1]["min_px"] == 4 and d[1]["yaml"]["min_px"] == 14 and d[1]["overridden"]


def test_a_model_without_scale_covers_every_scale():
    packages = {"any@1": pkg(scale=False)}
    routing = Routing.model_validate({"models": [{"model": "any@1"}]})
    assert choose(Router(), routing, packages, 0.5, 0).model == "any@1"


def test_the_px_of_a_model_follows_its_input_size():
    packages = {"big@1": pkg(min_px=20, imgsz=1920)}
    routing = Routing.model_validate({"models": [{"model": "big@1"}]})
    c = choose(Router(), routing, packages, 14, 0)  # 14 px at 1280 is 21 px at 1920
    assert c.model == "big@1" and c.px == pytest.approx(21)


@pytest.mark.parametrize("bad", [{"models": [{"model": "a", "min_px": -1}]},
                                 {"models": [{"model": "a"}, {"model": "a"}]},
                                 {"models": [], "margin": 1.0}, {"models": [], "hold_s": -1}])
def test_a_bad_routing_is_refused(bad):
    with pytest.raises(ValueError):
        Routing.model_validate(bad)


def test_routing_round_trips_without_empty_overrides():
    d = {"models": [{"model": "a@1"}, {"model": "b@1", "min_px": 6}], "margin": 0.2, "hold_s": 2.0}
    assert Routing.model_validate(d).model_dump(exclude_none=True) == d


def _stream(monkeypatch, packages, routing=None):
    from blade_detector import main as det
    from blade_detector.cluster import Cluster

    cluster = Cluster("one")
    cluster.routing = Routing.model_validate(routing) if routing else None
    monkeypatch.setattr(det.svc, "cluster", cluster)
    monkeypatch.setattr(det.svc, "packages", packages)
    monkeypatch.setattr(det.svc, "router", Router())
    monkeypatch.setattr(det.svc, "trackers", {})
    return det


def _env(px, sid="s1"):
    from blade_common.contracts import Envelope

    return Envelope(sid=sid, wid="w", data={"tel": tel_at(px), "jpeg": {"w": 1280, "h": 720}})


async def test_auto_stream_gets_empty_detections_where_no_model_covers_the_scale(monkeypatch):
    det = _stream(monkeypatch, {"near@1": pkg(min_px=14)},
                  {"models": [{"model": "near@1"}], "hold_s": 0.0})
    assert det.svc.model_for("s1") == "auto"
    assert det.svc.resolve("s1", _env(30), 0.0) == "near@1"
    assert det.svc.active_model("s1") == "near@1"
    env = _env(3)
    assert det.svc.resolve("s1", env, 1.0) == "none"
    (out,) = det.svc.process("none", [("s1", env, b"", 1.0)])
    assert out.data["model"] == "none" and out.data["dets"] == [] and out.data["tel"]["range_m"]
    assert det.svc.diag.snapshot().streams["s1"].skipped == 1
    assert det.svc.resolve("s1", _env(30), 2.0) == "near@1"


async def test_a_manual_model_is_not_routed(monkeypatch):
    det = _stream(monkeypatch, {"near@1": pkg(min_px=14), "b@1": pkg()},
                  {"models": [{"model": "near@1"}]})
    await det.set_model("s1", det.StreamModel(model="b@1"))
    assert det.svc.resolve("s1", _env(1), 0.0) == "b@1"
    await det.set_model("s1", det.StreamModel(model="auto"))
    assert det.svc.resolve("s1", _env(30), 1.0) == "near@1"


async def test_routing_is_set_read_and_reset_through_rest(monkeypatch):
    from fastapi import HTTPException

    det = _stream(monkeypatch, {"near@1": pkg(min_px=14), "far@1": pkg(min_px=6, max_px=14)})
    body = Routing(models=[Entry(model="near@1"), Entry(model="far@1", min_px=4)])
    got = await det.set_routing(body)
    assert [m["model"] for m in got["models"]] == ["near@1", "far@1"]
    far = got["models"][1]
    assert far["min_px"] == 4 and far["yaml"]["min_px"] == 6 and far["overridden"] and got["custom"]
    assert (await det.get_routing()) == got
    with pytest.raises(HTTPException) as e:
        await det.set_routing(Routing(models=[Entry(model="gone@1")]))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await det.set_routing(Routing(models=[Entry(model="far@1", min_px=9, max_px=5)]))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:  # max below the min_px of model.yaml (14) leaves no scale to cover
        await det.set_routing(Routing(models=[Entry(model="near@1", max_px=10)]))
    assert e.value.status_code == 400
    reset = await det.reset_routing()
    assert reset["models"][0]["model"] == det.AUTO_MODELS[0] and not reset["custom"]


async def test_routing_needs_the_model_on_every_live_replica(monkeypatch):
    from fastapi import HTTPException

    det = _stream(monkeypatch, {"near@1": pkg(min_px=14)})
    det.svc.cluster.live = ["one", "two"]
    det.svc.cluster.peers = {"one": {"models": ["near@1"]}, "two": {"models": []}}
    with pytest.raises(HTTPException) as e:
        await det.set_routing(Routing(models=[Entry(model="near@1")]))
    assert e.value.status_code == 409 and "two" in e.value.detail
    det.svc.cluster.peers["two"]["models"] = ["near@1"]
    assert (await det.set_routing(Routing(models=[Entry(model="near@1")])))["custom"]


async def test_any_replica_tells_what_auto_runs_for_a_stream(monkeypatch):
    det = _stream(monkeypatch, {"near@1": pkg(min_px=14)}, {"models": [{"model": "near@1"}]})
    det.svc.resolve("s1", _env(30), 0.0)  # this replica ("one") runs s1
    mine = await det.get_model("s1")
    assert mine["active"] == "near@1" and mine["px"] == pytest.approx(30, abs=0.1)
    monkeypatch.setattr(det.svc, "last_frame", {"s1": det.now()})
    assert det.svc.status()["auto"] == {"s1": {"active": "near@1", "px": mine["px"]}}  # what its heartbeat says
    det.svc.cluster.kv_det = object()  # with JetStream: s2 belongs to replica "two"
    det.svc.cluster.live = ["one", "two"]
    det.svc.cluster.assign = {"s2": {"instance": "two"}}
    det.svc.cluster.peers = {"two": {"auto": {"s2": {"active": None, "px": 3.0}}}}
    assert await det.get_model("s2") == {"sid": "s2", "model": "auto", "active": None, "px": 3.0}


@pytest.mark.parametrize("given", ["oracle", "oracle@1"])
async def test_the_oracle_has_one_id(monkeypatch, given):
    det = _stream(monkeypatch, {})
    assert (await det.set_model("s1", det.StreamModel(model=given)))["model"] == "oracle@1"
    assert (await det.get_model("s1"))["model"] == "oracle@1"
    monkeypatch.setattr(det, "DEFAULT_MODEL", given)
    assert det.svc.model_for("s2") == "oracle@1"


async def test_routing_set_without_its_kv_bucket_survives_the_refresh(monkeypatch):
    from blade_detector.cluster import Cluster

    class Kv:
        async def put(self, *_):
            pass

    async def stop(_):
        raise asyncio.CancelledError

    cl = Cluster("me")
    cl.kv_det = cl.kv_assign = Kv()
    cl.routing = Routing(models=[Entry(model="far@1")])
    monkeypatch.setattr("blade_detector.cluster.kv_read", lambda kv: _empty())
    monkeypatch.setattr("blade_detector.cluster.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await cl.run(dict)
    assert cl.routing == Routing(models=[Entry(model="far@1")])


async def _empty():
    return {}


def _package(root, name, yaml_text=None):
    root.mkdir(parents=True)
    (root / "model.yaml").write_text(yaml_text or f"name: {name}\nversion: 1\nclasses: [soldier]\n")
    (root / "model.onnx").write_bytes(b"")  # not opened until the model runs


def test_a_replica_loads_a_package_another_one_received(monkeypatch, tmp_path, caplog):
    det = _stream(monkeypatch, {})
    monkeypatch.setattr(det, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(det.svc, "tried", set())
    _package(tmp_path / "old_v1", "old")
    assert det.svc.load_new_packages() == ["old@1"]
    _package(tmp_path / ".upload-1", "half")  # a zip still being unpacked by POST /models
    _package(tmp_path / "bad_v1", "bad", yaml_text="name: bad\n")  # no classes
    assert det.svc.load_new_packages() == []
    _package(tmp_path / "new_v1", "new")
    assert det.svc.load_new_packages() == ["new@1"]
    assert set(det.svc.packages) == {"old@1", "new@1"}
    caplog.clear()
    assert det.svc.load_new_packages() == [] and "bad" not in caplog.text  # a bad package is reported once
