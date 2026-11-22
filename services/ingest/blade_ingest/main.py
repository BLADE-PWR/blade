"""ingest: the server's only way in for video, live or replayed: MPEG-TS over UDP/TCP :9000 or
POST /push/{sid}, sim truth over UDP :9100. Gives each live drone its world and stream id (worlds.py).
Publishes frames/truth/registry/events.<sid>, raw.<sid> (live bytes, for recording) and worlds.<wid>."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from starlette.requests import ClientDisconnect

from blade_common import ports
from blade_common.bus import close, connect, kv_bucket, now, publish
from blade_common.contracts import Envelope, FrameData, WorldInfo
from blade_common.service import Diag, env_float, env_int, run_app

from .source import Paired, PipeReader, Source
from .worlds import Worlds

log = logging.getLogger("ingest")

VIDEO_PORT = env_int("VIDEO_PORT", ports.VIDEO_UDP)
TRUTH_PORT = env_int("TRUTH_PORT", ports.TRUTH_UDP)
IDLE_S = env_float("SOURCE_IDLE_S", 3.0)
EXPIRE_S = env_float("EXPIRE_S", 600.0)  # a live world without frames this long ends (its objects have all expired)
REPLAY_GRACE_S = 5.0  # a replay world with no push open this long ends (a drone briefly has none between segments)
RAW_CHUNK = 64 * 1024
RAW_FLUSH_S = 0.1


def frame_ts(kind: str, tel: dict, t_in: float, t_out: float) -> dict:
    """-> a frame's stamps: capture (grab time live, arrival in a replay) and, in a replay, its recording time"""
    cap = tel.get("t_capture") if kind == "live" else None
    ts = {"capture": round(cap or t_in, 6), "ingest": [round(t_in, 6), round(t_out, 6)]}
    if kind != "live":  # a recording without times (stills) runs on the arrival, but not as a live stream
        ts["media"] = round(tel.get("t_capture") or t_in, 6)
    return ts


class Ingest:
    def __init__(self):
        self.sources: dict[str, Source] = {}
        self.diag = Diag("ingest")
        self.nc = None
        self.kv = None
        self.worlds = Worlds(alive=lambda key: (s := self.sources.get(key)) is not None and not s.done.is_set())
        self.loop: asyncio.AbstractEventLoop | None = None
        self._raw: dict[str, list] = {}  # source key -> [bytearray, t_first]
        self._raw_lock = threading.Lock()
        self._stop = threading.Event()

    def _spawn(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def assign(self, src: Source, tel: dict) -> None:
        """Give a source its world and stream id from the names in its telemetry; a live one again when they change
        (the drone went to another map). A replay keeps the ids it was pushed with and is only registered."""
        mission, map_name, asked = tel.get("mission") or "default", tel.get("map") or "unknown", src.name(tel)
        want = (mission, map_name, asked)
        if src.joined == want or (src.kind != "live" and src.joined is not None):
            return
        if src.kind == "live":
            if src.sid is not None:  # bytes buffered so far belong to the old stream id
                self._flush_raw(src, force=True)
            if src.joined is not None:
                self.worlds.leave(src.wid, src.sid)
            j = self.worlds.join_live(mission, map_name, asked, src.key)
        else:
            j = self.worlds.join_replay(src.wid, src.sid, mission, map_name, asked)
        src.joined, src.sid, src.wid, src.drone = want, j.sid, j.world.wid, j.name
        log.info("%s: drone %s is %s in world %s (mission %s, map %s)", src.key, j.name, j.sid, j.world.wid,
                 mission, map_name)
        if j.changed:
            self._spawn(self.announce(j.world.info()))

    async def announce(self, info: dict) -> None:
        data = WorldInfo.model_validate(info).model_dump()
        await publish(self.nc, f"worlds.{info['wid']}", Envelope(wid=info["wid"], data=data))

    async def end_world(self, wid: str, why: str) -> bool:
        """End a world: services drop it. Its drones still sending start a new one with their next frame."""
        w = self.worlds.end(wid)
        if w is None:
            return False
        for src in list(self.sources.values()):
            if src.kind == "live" and src.wid == wid:
                src.joined = None  # assigned afresh by its next frame
        log.info("world %s (mission %s, map %s) ended: %s", wid, w.mission, w.map, why)
        await self.announce(w.info())
        return True

    async def reap_worlds(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            pushed = {s.wid for s in list(self.sources.values()) if s.kind == "replay" and not s.done.is_set()}
            for wid in self.worlds.idle(time.time(), EXPIRE_S, pushed, REPLAY_GRACE_S):
                await self.end_world(wid, "nothing for a while")

    def on_frame(self, src: Source, p: Paired) -> None:
        tel = p.tel
        self.assign(src, tel)
        self.worlds.seen(src.wid, p.t_in)
        tel.pop("sid", None)  # recordings made before names and ids were split
        tel["drone"] = src.drone
        ts = frame_ts(src.kind, tel, p.t_in, p.t_out)
        env = Envelope(sid=src.sid, frame=tel.get("frame"), wid=src.wid, ts=ts,
                       data=FrameData.model_validate({"tel": tel, "jpeg": {"w": p.jpeg_size[0],
                                                                         "h": p.jpeg_size[1]}}).model_dump(
                           exclude_none=True))
        self.diag.got(src.sid)
        self.diag.sent(src.sid, latency_s=p.t_out - ts["capture"])
        if src.kind == "live" and tel.get("t_capture"):
            self.diag.clock(src.sid, p.t_in - tel["t_capture"])
        self._spawn(publish(self.nc, f"frames.{src.sid}", env, p.jpeg))

    def on_sid(self, src: Source) -> None:
        log.info("%s is stream %s (%s)", src.key, src.sid, src.kind)
        if src.kind == "live":
            self._flush_raw(src, force=True)

    def new_source(self, key: str, kind: str, sid: str | None = None, wid: str | None = None) -> Source:
        # a short pipe for replays keeps their truth close to what is being decoded
        src = Source(key=key, kind=kind, sid_override=sid, wid=wid,
                     pipe=PipeReader(16 if kind == "replay" else 256))
        src.on_frame = lambda p, s=src: self.on_frame(s, p)
        src.assign = self.assign
        src.on_sid = self.on_sid
        self.sources[key] = src
        return src

    def _raw_add(self, src: Source, data: bytes, t: float) -> None:
        with self._raw_lock:
            slot = self._raw.setdefault(src.key, [bytearray(), t])
            if not slot[0]:
                slot[1] = t
            slot[0] += data
            if len(slot[0]) > 8 << 20:  # no sid yet after 8 MB: not a ZPI stream
                slot[0] = slot[0][-(4 << 20):]
        self._flush_raw(src)

    def _flush_raw(self, src: Source, force: bool = False) -> None:
        with self._raw_lock:
            slot = self._raw.get(src.key)
            if not slot or not slot[0] or src.sid is None:
                return
            if not force and len(slot[0]) < RAW_CHUNK and time.time() - slot[1] < RAW_FLUSH_S:
                return
            data, t = bytes(slot[0]), slot[1]
            slot[0] = bytearray()
        for i in range(0, len(data), 512 * 1024):
            self._spawn(self.nc.publish(f"raw.{src.sid}", data[i:i + 512 * 1024],
                                        headers={"t": f"{t:.6f}", "src": src.key, "wid": src.wid or "",
                                                 "drone": src.drone or src.sid,
                                                 "mission": src.joined[0] if src.joined else "",
                                                 "map": src.joined[1] if src.joined else ""}))

    def video_udp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 << 20)
        sock.bind(("0.0.0.0", VIDEO_PORT))
        sock.settimeout(0.2)
        log.info("video: udp :%d", VIDEO_PORT)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(65536)
            except TimeoutError:
                self._reap()
                continue
            t = time.time()
            key = f"udp:{addr[0]}:{addr[1]}"
            src = self.sources.get(key)
            if src is None or src.done.is_set():
                src = self.new_source(key, "live")
            if not src.feed(data):
                self.diag.drop(src.sid or key)  # decoder behind: drop rather than stall the socket
            self._raw_add(src, data, t)

    def video_tcp(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", VIDEO_PORT))
        srv.listen(16)
        srv.settimeout(0.5)
        log.info("video: tcp :%d", VIDEO_PORT)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except TimeoutError:
                continue
            threading.Thread(target=self._tcp_conn, args=(conn, addr), name=f"tcp-{addr[0]}:{addr[1]}",
                             daemon=True).start()

    def _tcp_conn(self, conn: socket.socket, addr) -> None:
        key = f"tcp:{addr[0]}:{addr[1]}"
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        conn.settimeout(1.0)
        log.info("%s connected", key)
        src = self.new_source(key, "live")
        try:
            while not self._stop.is_set():
                try:
                    data = conn.recv(1 << 16)
                except TimeoutError:
                    continue
                if not data:
                    break
                if src.done.is_set():  # reaped while the uplink paused: a new stream
                    src = self.new_source(key, "live")
                src.feed(data, block=True)
                self._raw_add(src, data, time.time())
        except OSError as e:
            log.info("%s: %s", key, e)
        finally:
            conn.close()
            self._flush_raw(src, force=True)
            src.close()
            log.info("%s disconnected", key)

    def _reap(self) -> None:
        t = time.time()
        for key, src in list(self.sources.items()):
            if src.kind == "live" and not src.done.is_set() and t - src.last_seen > IDLE_S:
                self._flush_raw(src, force=True)
                src.close()
            self._flush_raw(src)
            if src.done.is_set() and t - src.last_seen > 60:
                del self.sources[key]
                self._raw.pop(key, None)

    def truth_udp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        sock.bind(("0.0.0.0", TRUTH_PORT))
        sock.settimeout(0.5)
        log.info("sim truth: udp :%d", TRUTH_PORT)
        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(65536)
                msg = json.loads(data)
            except (TimeoutError, ValueError):
                continue
            self._spawn(self.publish_truth(msg, addr[0]))

    async def publish_truth(self, msg: dict, host: str = "") -> None:
        """An uplink's truth names its drone and mission; a replay's carries the ids it plays under."""
        kind, rec = msg.get("kind"), msg.get("rec") or {}
        if msg.get("drone"):
            ids = self.worlds.truth_ids(msg.get("mission") or "default", msg["drone"], host)
            if ids is None:  # before the drone's first frame; the uplink resends spawns every few seconds
                return
            wid, sid = ids
        else:
            sid, wid = msg.get("sid"), msg.get("wid")
        if not sid or not wid or kind not in ("gt", "spawn", "evt"):
            return
        env = Envelope(sid=sid, frame=rec.get("frame"), wid=wid, ts={"truth_recv": round(now(), 6)}, data=rec)
        subject = {"gt": "truth", "spawn": "registry", "evt": "events"}[kind]
        await publish(self.nc, f"{subject}.{sid}", env)
        if kind == "spawn" and self.kv is not None:
            try:
                await self.kv.put(f"{sid}.{rec['id']}", json.dumps(rec).encode())
            except Exception as e:
                log.debug("registry kv put failed: %s", e)


svc = Ingest()


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc.loop = asyncio.get_running_loop()
    svc.nc = await connect("ingest")
    try:
        svc.kv = await kv_bucket(svc.nc.jetstream(), "registry")
    except Exception as e:
        log.warning("JetStream KV unavailable (%s): spawn registry is not persisted", e)
    threading.Thread(target=svc.video_udp, name="video-udp", daemon=True).start()
    threading.Thread(target=svc.video_tcp, name="video-tcp", daemon=True).start()
    threading.Thread(target=svc.truth_udp, name="truth-udp", daemon=True).start()
    tasks = [asyncio.create_task(svc.diag.run(svc.nc)), asyncio.create_task(svc.reap_worlds())]
    yield
    svc._stop.set()
    for t in tasks:
        t.cancel()
    await close(svc.nc)


app = FastAPI(title="blade ingest", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "nats": bool(svc.nc and svc.nc.is_connected), "kv": svc.kv is not None}


@app.get("/streams")
async def streams(all: bool = False):
    return [s.info() for s in svc.sources.values() if all or not s.done.is_set()]


@app.get("/worlds")
async def worlds(mission: str):
    return svc.worlds.of_mission(mission)


@app.post("/missions/{mission}/stop")
async def stop_mission(mission: str):
    """End every live world of the mission now, instead of after EXPIRE_S: services drop its objects and alerts, and
    its recordings are closed. Drones still sending start the mission afresh, with new ids."""
    ended = [w["wid"] for w in svc.worlds.of_mission(mission)
             if await svc.end_world(w["wid"], "stopped by the operator")]
    return {"mission": mission, "ended": ended}


@app.post("/push/{sid}")
async def push(sid: str, request: Request, wid: str):
    """Stream an MPEG-TS body in. Backpressure: the body is read only as fast as it is decoded."""
    key = f"push:{sid}"
    old = svc.sources.get(key)
    if old and not old.done.is_set():
        raise HTTPException(409, f"{sid} is already being pushed")
    src = svc.new_source(key, "replay", sid=sid, wid=wid)
    n = 0
    try:
        async for chunk in request.stream():
            if chunk:
                n += len(chunk)
                await asyncio.to_thread(src.feed, chunk, True)
    except ClientDisconnect:
        log.info("%s: pusher went away after %.1f MB", sid, n / 1e6)
    finally:
        await asyncio.to_thread(src.close)
        await asyncio.to_thread(src.done.wait, 30)
    return {"sid": sid, "bytes": n, "frames": src.frames, "unpaired": src.unpaired}


def main() -> None:
    run_app("blade_ingest.main:app", ports.INGEST)
