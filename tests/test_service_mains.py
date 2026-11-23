"""The services' main modules (bus callbacks, REST, the gateway's WebSocket) without NATS or other services: a fake
bus delivers messages to the callbacks subscribed to them and keeps what they publish; HTTP to other services goes to
an httpx MockTransport."""
import asyncio
import io
import json
import struct

import httpx
import numpy as np
import pytest
from conftest import look_at_tel, write_map
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from PIL import Image

import blade_evaluator.main as em
import blade_gateway.main as gm
import blade_gateway.sessions as gs
import blade_georef.main as grm
from blade_common.bus import ENV_HEADER, decode
from blade_common.contracts import Envelope
from blade_geometry import MapPackage


class FakeMsg:
    def __init__(self, subject: str, data: bytes, headers: dict | None = None):
        self.subject, self.data, self.headers = subject, data, headers


def matches(pattern: str, subject: str) -> bool:
    p, s = pattern.split("."), subject.split(".")
    for i, tok in enumerate(p):
        if tok == ">":
            return len(s) > i
        if i >= len(s) or (tok != "*" and tok != s[i]):
            return False
    return len(p) == len(s)


def raw(subject: str, data: dict, **kw) -> FakeMsg:
    """A message as bus.publish sends one without a binary body."""
    return FakeMsg(subject, Envelope(data=data, **kw).model_dump_json().encode())


class FakeSub:
    def __init__(self, nc, pattern, cb):
        self.nc, self.pattern, self.cb = nc, pattern, cb

    async def unsubscribe(self):
        self.nc.subs.remove(self)


class FakeNats:
    is_connected = True

    def __init__(self):
        self.subs: list[FakeSub] = []
        self.published: list[tuple[str, Envelope, bytes | None]] = []

    async def publish(self, subject, data, headers=None):
        env, blob = decode(FakeMsg(subject, data, headers))
        self.published.append((subject, env, blob))

    async def subscribe(self, subject, cb):
        sub = FakeSub(self, subject, cb)
        self.subs.append(sub)
        return sub

    async def deliver(self, subject: str, env: Envelope, blob: bytes | None = None) -> None:
        """As bus.publish sends it: the envelope as the body, or in a header next to a binary body."""
        text = env.model_dump_json(exclude_none=True)
        msg = FakeMsg(subject, text.encode()) if blob is None else FakeMsg(subject, blob, {ENV_HEADER: text})
        for sub in [s for s in self.subs if matches(s.pattern, subject)]:
            await sub.cb(msg)


def test_fake_subjects_match_like_nats():
    assert matches("geo.>", "geo.d-1") and not matches("geo.>", "geo")
    assert matches("world.*.objects", "world.w-1.objects") and not matches("world.*.objects", "world.w-1.events")
    assert matches("worlds.*", "worlds.w-1") and not matches("worlds.*", "worlds.w-1.x")


async def until(cond, timeout_s: float = 2.0) -> None:
    for _ in range(int(timeout_s / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


# ---------------------------------------------------------------- evaluator

FOOTPRINT = [[0, 0], [20, 0], [20, 20], [0, 20]]


async def test_evaluator_scores_objects_in_view_and_lists_the_rest(monkeypatch, tmp_path):
    svc = em.Evaluator()
    svc.nc = FakeNats()
    monkeypatch.setattr(em, "svc", svc)  # what the REST routes read
    monkeypatch.setattr(em, "EVAL_DIR", tmp_path)

    def env(data, subject, **kw):
        return raw(subject, data, sid="d-1", wid="w-1", **kw)

    for oid, cls in ((1, "soldier"), (2, "tracked_vehicle"), (3, "soldier")):
        await svc.on_truth(env({"id": oid, "cls": cls}, subject="registry.d-1"))
    await svc.on_truth(env({"frame": 10, "objs": [[1, [5, 5, 0]], [2, [12, 12, 0]], [3, [500, 500, 0]]]},
                           subject="truth.d-1"))
    # georef: the footprint, and one point 1 m from soldier 1
    await svc.on_geo(env({"footprint": FOOTPRINT, "pts": [{"grp": "soldier", "xyz": [6, 5, 0]}]},
                         subject="geo.d-1", frame=10))
    await svc.on_world(raw("world.w-1.objects", wid="w-1", data={"objects": [
        {"oid": "o1", "grp": "soldier", "xy": [5, 6], "state": "active"},
        {"oid": "o2", "grp": "soldier", "xy": [18, 18], "state": "active"},  # in view, no truth near: false
        {"oid": "o3", "grp": "tank", "xy": [12, 12], "state": "stale"},  # not active: not matched
        {"oid": "o4", "grp": "soldier", "xy": [100, 100], "state": "active"}]}))
    await svc.on_world(raw("world.w-1.objects", wid="w-1", data={"objects": [], "expired": ["o4"]}))
    assert "o4" not in svc.objects["w-1"]

    await svc.evaluate("w-1", 100.0)
    subject, out, _ = svc.nc.published[-1]
    assert subject == "eval.w-1"
    truth = {t["truth_id"]: t for t in out.data["truth"]}
    assert truth[1]["oid"] == "o1" and truth[1]["err_m"] == 1.0 and truth[1]["in_view"]
    assert truth[2]["grp"] == "tank" and "oid" not in truth[2] and truth[2]["in_view"]  # in view, missed
    assert truth[3]["in_view"] is False and "oid" not in truth[3]  # out of view: listed, not scored
    groups = out.data["groups"]
    assert groups["soldier"]["detected"] == 1 and groups["soldier"]["in_view"] == 1  # soldier 3 does not count
    assert groups["tank"]["detected"] == 0 and groups["tank"]["in_view"] == 1
    assert out.data["false_objects"] == ["o2"]

    for f, _ in svc.writers.values():
        f.flush()
    rows = next(tmp_path.glob("eval_w-1_*.csv")).read_text().splitlines()
    assert rows[0].startswith("t,level") and [r.split(",")[1] for r in rows[1:]] == ["geo", "world"]

    api = TestClient(em.app)  # no lifespan: no NATS
    assert api.get("/health").json()["worlds"] == ["w-1"]
    win = api.get("/worlds/w-1/eval").json()
    assert win["world"]["soldier"]["n"] == 1 and win["geo"]["soldier"]["cep50_m"] == 1.0
    assert api.get("/worlds/w-1/eval/report").json()["world"]["soldier"]["n"] == 1
    assert api.post("/worlds/w-1/eval/reset").json()["reset"]
    assert api.get("/worlds/w-1/eval").status_code == 404

    def info(ended):
        return raw("worlds.w-1", {"wid": "w-1", "ended": ended}, wid="w-1")

    await svc.evaluate("w-1", 101.0)
    await svc.on_world_info(info(True))  # the world ended: everything of it is dropped
    assert not svc.evals and not svc.writers and "d-1" not in svc.sid_wid and "d-1" not in svc.truth
    await svc.on_truth(env({"id": 9, "cls": "soldier"}, subject="registry.d-1"))  # late: ignored
    assert "d-1" not in svc.truth
    await svc.on_world_info(info(False))  # a replay world back under its id
    assert "w-1" not in svc.ended


# ---------------------------------------------------------------- georef

CENTRE = (960.0, 960.0)


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    n = 256
    yy, xx = np.mgrid[0:n, 0:n]
    d = write_map(tmp_path_factory.mktemp("maps") / "tiny", 80 + 20 * np.sin(xx / 15.0) * np.cos(yy / 21.0))
    return MapPackage.load(d)


async def test_georef_places_detections_and_still_publishes_on_an_unknown_map(monkeypatch, tiny):
    nc = FakeNats()
    monkeypatch.setattr(grm, "state", {"nc": nc})
    monkeypatch.setattr(grm, "packages", {"tiny": tiny})
    monkeypatch.setattr(grm, "missing", set())
    tel = look_at_tel(tiny.terrain, CENTRE, 5)  # aimed at CENTRE: the image centre (320, 180) is on it
    det = {"tid": 1, "cls": "soldier_standing", "grp": "soldier", "conf": 0.9, "box": [315, 170, 325, 180]}
    await nc.subscribe("detections.>", grm.on_detections)
    await nc.deliver("detections.d-1", Envelope(sid="d-1", wid="w-1", frame=5, ts={"capture": 1.0},
                                                data={"tel": tel, "dets": [det]}))
    subject, geo, _ = nc.published[-1]
    assert subject == "geo.d-1" and geo.frame == 5 and "georef" in geo.ts and geo.data["map"] == "tiny"
    assert np.allclose(geo.data["pts"][0]["xyz"][:2], CENTRE, atol=1.0) and len(geo.data["footprint"]) == 4

    for _ in range(2):  # no package for its map: published without points (the world still sees the drone), logged once
        await nc.deliver("detections.d-2", Envelope(sid="d-2", wid="w-2", data={"tel": {**tel, "map": "nowhere"},
                                                                                 "dets": [det]}))
    _, geo, _ = nc.published[-1]
    assert geo.data["pts"] == [] and geo.data["footprint"] == [] and grm.missing == {"nowhere"}

    api = TestClient(grm.app)
    r = api.post("/project", json={"tel": tel, "pixels": [[320, 180]]})
    assert np.allclose(r.json()["pts"][0]["xyz"][:2], CENTRE, atol=1.0)
    assert api.post("/project", json={"tel": {**tel, "map": "nowhere"}}).status_code == 404
    assert api.get("/health").json() == {"ok": True, "maps": {"tiny": list(tiny.terrain.extent)}}


# ---------------------------------------------------------------- gateway

class FakeWebSocket:
    def __init__(self):
        self.sent: list = []
        self.closed: tuple | None = None
        self.gone = asyncio.Event()  # set: the client leaves

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        self.sent.append(data)

    async def receive_text(self):
        await self.gone.wait()
        raise WebSocketDisconnect()

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    def of(self, typ):
        return [m for m in self.sent if isinstance(m, dict) and m["type"] == typ]


def mock_http(routes: dict, seen: list | None = None) -> httpx.MockTransport:
    """path -> JSON reply (or a callable taking the request); any other path: 404."""
    def handle(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(req)
        reply = routes.get(req.url.path)
        if reply is None:
            return httpx.Response(404, json={"detail": "not found"})
        return reply(req) if callable(reply) else httpx.Response(200, json=reply)
    return httpx.MockTransport(handle)


def jpeg(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (90, 120, 60)).save(buf, "JPEG")
    return buf.getvalue()


def world_info(wid, mission="m1", ended=False):
    return {"wid": wid, "mission": mission, "map": "tiny", "kind": "live", "drones": {}, "ended": ended}


async def test_gateway_mission_socket_follows_the_worlds_of_its_mission(monkeypatch):
    nc = FakeNats()
    http = httpx.AsyncClient(transport=mock_http({
        "/worlds": [world_info("w-1")],  # ingest: the mission's live worlds now
        "/worlds/w-1/snapshot": {"objects": []}, "/worlds/w-2/snapshot": {"objects": []}}))
    monkeypatch.setattr(gm, "state", {"nc": nc, "http": http})
    sock = FakeWebSocket()
    task = asyncio.create_task(gm.ws(sock, mission="m1"))
    await until(lambda: sock.of("snapshot"))
    assert sock.of("world")[0]["wid"] == "w-1" and sock.of("snapshot")[0]["wid"] == "w-1"

    await nc.deliver("detections.d-1", Envelope(sid="d-1", wid="w-1", data={"tel": {"map": "tiny"}, "dets": []}))
    await nc.deliver("detections.d-9", Envelope(sid="d-9", wid="w-9", data={"dets": []}))  # another mission's
    await until(lambda: sock.of("detections"))
    assert [m["sid"] for m in sock.of("detections")] == ["d-1"] and "tel" not in sock.of("detections")[0]["data"]

    for _ in range(2):  # the second comes too soon after the first: no preview for it
        await nc.deliver("frames.d-1", Envelope(sid="d-1", wid="w-1", frame=3, data={"tel": {"drone": "uav1"}}),
                         jpeg(1920, 1080))
    await until(lambda: any(isinstance(m, bytes) for m in sock.sent))
    await asyncio.sleep(0.05)
    frames = [m for m in sock.sent if isinstance(m, bytes)]
    assert len(frames) == 1 and frames[0][:4] == b"ZPIF"
    n = struct.unpack(">I", frames[0][4:8])[0]
    head = json.loads(frames[0][8:8 + n])
    assert head["drone"] == "uav1" and head["w"] == gm.PREVIEW_W and head["h"] == 540

    await nc.deliver("worlds.w-2", Envelope(wid="w-2", data=world_info("w-2")))  # the mission on another map
    await nc.deliver("worlds.w-3", Envelope(wid="w-3", data=world_info("w-3", mission="other")))
    await until(lambda: len(sock.of("snapshot")) == 2)
    assert [m["wid"] for m in sock.of("world")] == ["w-1", "w-2"]
    await nc.deliver("world.w-2.objects", Envelope(wid="w-2", data={"objects": []}))
    await until(lambda: sock.of("world.objects"))

    await nc.deliver("worlds.w-1", Envelope(wid="w-1", data=world_info("w-1", ended=True)))
    await until(lambda: len(sock.of("world")) == 3)
    await nc.deliver("world.w-1.objects", Envelope(wid="w-1", data={"objects": []}))  # not watched any more
    await nc.deliver("world.w-2.objects", Envelope(wid="w-2", data={"objects": []}))
    await until(lambda: len(sock.of("world.objects")) == 2)
    assert {m["wid"] for m in sock.of("world.objects")} == {"w-2"}

    sock.gone.set()
    await asyncio.wait_for(task, 2)
    assert nc.subs == []  # every subscription of the socket is gone with it
    await http.aclose()


async def test_gateway_session_socket_counts_its_viewers(monkeypatch):
    nc = FakeNats()
    orch = gs.Orchestrator(gm.URLS)
    left = []

    async def unwatched(s):
        orch.viewers[s.id] -= 1
        left.append(s.id)

    monkeypatch.setattr(orch, "unwatched", unwatched)
    monkeypatch.setattr(gm, "orch", orch)
    http = httpx.AsyncClient(transport=mock_http({"/worlds/rw-1/snapshot": {"objects": []}}))
    monkeypatch.setattr(gm, "state", {"nc": nc, "http": http})
    replay = gs.ReplaySpec(recording="rec", wid="rw-1", sids={"uav1": "rp-1"})
    s = orch.create(gs.SessionSpec(name="r", replays=[replay]))

    sock = FakeWebSocket()
    task = asyncio.create_task(gm.ws(sock, session=s.id))
    await until(lambda: orch.viewers[s.id] == 1)
    assert sock.sent[0]["type"] == "session" and sock.sent[0]["data"]["id"] == s.id
    await nc.deliver("geo.rp-1", Envelope(sid="rp-1", wid="rw-1", data={"pts": []}))
    await until(lambda: sock.of("geo"))
    sock.gone.set()
    await asyncio.wait_for(task, 2)
    assert left == [s.id] and nc.subs == []

    lost = FakeWebSocket()
    await gm.ws(lost, session="nope")
    assert lost.closed[0] == 4404
    await http.aclose()


def test_gateway_rest_sessions_replay_control_and_proxy(monkeypatch):
    seen: list[httpx.Request] = []
    routes = {
        "/replays/rw-1": lambda req: httpx.Response(200, json={"wid": "rw-1", **json.loads(req.content or b"{}")}),
        "/replays": {"wid": "rw-1"},
        "/streams": [{"sid": "d-1"}],
        "/recordings": [{"id": "rec"}],
        "/maps/tiny/tile": lambda req: httpx.Response(200, content=b"png", headers={"x-layer": "sat",
                                                                                      "x-secret": "no"}),
    }
    transport = mock_http(routes, seen)
    monkeypatch.setattr(gm, "state", {"nc": None, "http": httpx.AsyncClient(transport=transport)})
    real = httpx.AsyncClient
    monkeypatch.setattr(gs.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))  # the orchestrator's
    monkeypatch.setattr(gm, "orch", gs.Orchestrator(gm.URLS))
    api = TestClient(gm.app)  # no lifespan: no NATS

    assert api.get("/health").json()["nats"] is False
    s = api.post("/sessions", json={"name": "r", "replays": [{"recording": "rec", "wid": "rw-1",
                                                              "sids": {"uav1": "rp-1"}}]}).json()
    assert [x["id"] for x in api.get("/sessions").json()] == [s["id"]]
    assert api.get(f"/sessions/{s['id']}").json()["name"] == "r"
    assert api.get("/sessions/nope").status_code == 404

    started = api.post(f"/sessions/{s['id']}/start").json()
    assert started["state"] == "running" and started["errors"] == []
    assert [(r.method, r.url.path) for r in seen] == [("DELETE", "/replays/rw-1"), ("POST", "/replays")]
    r = api.patch(f"/sessions/{s['id']}/replays/rw-1", json={"paused": True})
    assert r.json() == {"wid": "rw-1", "paused": True}
    assert api.patch(f"/sessions/{s['id']}/replays/rw-9", json={"paused": True}).status_code == 404
    assert api.post(f"/sessions/{s['id']}/stop").json()["state"] == "stopped"
    assert api.delete(f"/sessions/{s['id']}").status_code == 204
    assert api.get("/sessions").json() == []

    assert api.get("/streams").json() == [{"sid": "d-1"}]
    r = api.get("/api/recordings", params={"q": "x"})
    assert r.json() == [{"id": "rec"}] and seen[-1].url.params["q"] == "x"
    r = api.get("/api/maps/tiny/tile")
    assert r.content == b"png" and r.headers["x-layer"] == "sat" and "x-secret" not in r.headers
    assert api.get("/api/nope").status_code == 404
