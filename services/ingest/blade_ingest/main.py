"""ingest: the server's only way in for video, live or replayed: MPEG-TS over UDP/TCP :9000 or
POST /push/{sid} (?delivery=all waits for ack.<sid> of the previous frame), sim truth over UDP :9100.
Publishes frames/truth/registry/events.<sid> and raw.<sid> (live bytes, for recording)."""
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
from blade_common.contracts import Envelope, FrameData
from blade_common.service import Diag, env_float, env_int, run_app

from .source import Paired, PipeReader, Source

log = logging.getLogger("ingest")

VIDEO_PORT = env_int("VIDEO_PORT", ports.VIDEO_UDP)
TRUTH_PORT = env_int("TRUTH_PORT", ports.TRUTH_UDP)
IDLE_S = env_float("SOURCE_IDLE_S", 3.0)
ACK_TIMEOUT_S = env_float("ACK_TIMEOUT_S", 10.0)
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
        self.loop: asyncio.AbstractEventLoop | None = None
        self._raw: dict[str, list] = {}  # source key -> [bytearray, t_first]
        self._raw_lock = threading.Lock()
        self._stop = threading.Event()
        self.acked: dict[str, int] = {}
        self._ack = threading.Condition()

    def _spawn(self, coro) -> None:
        asyncio.run_coroutine_threadsafe(coro, self.loop)

    def on_frame(self, src: Source, p: Paired) -> None:
        tel = p.tel
        if src.wid is None:  # live: the mission the uplink was started with; a replay has its own
            src.wid = tel["mission"]
            log.info("%s (%s) joins world %s", src.key, src.sid, src.wid)
        ts = frame_ts(src.kind, tel, p.t_in, p.t_out)
        env = Envelope(sid=p.sid, frame=tel.get("frame"), wid=src.wid, ts=ts,
                       data=FrameData.model_validate({"tel": tel, "jpeg": {"w": p.jpeg_size[0],
                                                                         "h": p.jpeg_size[1]}}).model_dump(
                           exclude_none=True))
        self.diag.got(p.sid)
        self.diag.sent(p.sid, latency_s=p.t_out - ts["capture"])
        if src.kind == "live" and tel.get("t_capture"):
            self.diag.clock(p.sid, p.t_in - tel["t_capture"])
        if src.delivery == "all":
            env.delivery = "all"
        self._spawn(publish(self.nc, f"frames.{p.sid}", env, p.jpeg))
        if src.delivery == "all" and env.frame is not None:
            with self._ack:  # demux thread waits: backpressure up to the replay
                if not self._ack.wait_for(lambda: self.acked.get(p.sid, -1) >= env.frame, ACK_TIMEOUT_S):
                    log.warning("%s: no ack for frame %s within %.0f s, going on", p.sid, env.frame, ACK_TIMEOUT_S)

    async def on_ack(self, msg) -> None:
        d = json.loads(msg.data)
        with self._ack:
            self.acked[d["sid"]] = max(self.acked.get(d["sid"], -1), int(d["frame"]))
            self._ack.notify_all()

    def on_sid(self, src: Source) -> None:
        log.info("%s is stream %s (%s)", src.key, src.sid, src.kind)
        if src.kind == "live":
            self._flush_raw(src, force=True)

    def new_source(self, key: str, kind: str, sid: str | None = None, wid: str | None = None,
                   delivery: str = "latest") -> Source:
        # a short pipe for replays keeps their truth close to what is being decoded
        src = Source(key=key, kind=kind, sid_override=sid, wid=wid, delivery=delivery,
                     pipe=PipeReader(16 if kind == "replay" else 256))
        src.on_frame = lambda p, s=src: self.on_frame(s, p)
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
                                        headers={"t": f"{t:.6f}", "src": src.key}))

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
                msg = json.loads(sock.recvfrom(65536)[0])
            except (TimeoutError, ValueError):
                continue
            self._spawn(self.publish_truth(msg))

    async def publish_truth(self, msg: dict) -> None:
        sid, wid, kind, rec = msg.get("sid"), msg.get("wid"), msg.get("kind"), msg.get("rec") or {}
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
    await svc.nc.subscribe("ack.>", cb=svc.on_ack)
    threading.Thread(target=svc.video_udp, name="video-udp", daemon=True).start()
    threading.Thread(target=svc.video_tcp, name="video-tcp", daemon=True).start()
    threading.Thread(target=svc.truth_udp, name="truth-udp", daemon=True).start()
    diag = asyncio.create_task(svc.diag.run(svc.nc))
    yield
    svc._stop.set()
    diag.cancel()
    await close(svc.nc)


app = FastAPI(title="blade ingest", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "nats": bool(svc.nc and svc.nc.is_connected), "kv": svc.kv is not None}


@app.get("/streams")
async def streams(all: bool = False):
    return [s.info() for s in svc.sources.values() if all or not s.done.is_set()]


@app.post("/push/{sid}")
async def push(sid: str, request: Request, wid: str, delivery: str = "latest"):
    """Stream an MPEG-TS body in. Backpressure: the body is read only as fast as it is decoded."""
    key = f"push:{sid}"
    old = svc.sources.get(key)
    if old and not old.done.is_set():
        raise HTTPException(409, f"{sid} is already being pushed")
    if delivery == "all":
        svc.acked.pop(sid, None)
    src = svc.new_source(key, "replay", sid=sid, wid=wid, delivery=delivery)
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
