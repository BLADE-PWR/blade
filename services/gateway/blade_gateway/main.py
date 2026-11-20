"""gateway: operator sessions, /api/<area>/... proxied to the services (PROXIES), and a WebSocket per
session (/ws?session=<id> or ?sids=a,b&wids=x,y): JSON text messages, plus binary frames
(b"ZPIF" + uint32 header length + header JSON + JPEG)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from PIL import Image
from pydantic import BaseModel

from blade_common import ports
from blade_common.bus import close, connect, decode
from blade_common.service import env_float, env_int, env_str, run_app

from .sessions import Orchestrator, SessionSpec

log = logging.getLogger("gateway")

URLS = {
    "ingest": env_str("INGEST_URL", f"http://127.0.0.1:{ports.INGEST}"),
    "sessions": env_str("SESSIONS_URL", f"http://127.0.0.1:{ports.SESSIONS}"),
    "detector": env_str("DETECTOR_URL", f"http://127.0.0.1:{ports.DETECTOR}"),
    "world": env_str("WORLD_URL", f"http://127.0.0.1:{ports.WORLD}"),
    "terrain": env_str("TERRAIN_URL", f"http://127.0.0.1:{ports.TERRAIN}"),
    "evaluator": env_str("EVALUATOR_URL", f"http://127.0.0.1:{ports.EVALUATOR}"),
    "decision": env_str("DECISION_URL", f"http://127.0.0.1:{ports.DECISION}"),
}
PREVIEW_FPS = env_float("PREVIEW_FPS", 15.0)
PREVIEW_W = env_int("PREVIEW_W", 960)
PROXIES = {"models": ("detector", "/models"), "detector": ("detector", "/streams"),
           "recordings": ("sessions", "/recordings"), "replays": ("sessions", "/replays"), "maps": ("terrain", "/maps"),
           "worlds": ("world", "/worlds"), "decision": ("decision", "/worlds"), "eval": ("evaluator", "/worlds")}

orch = Orchestrator(URLS, Path(env_str("SESSIONS_STORE", "state/gateway_sessions.json")))
state: dict = {"nc": None, "http": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["nc"] = await connect("gateway")
    orch.nc = state["nc"]
    state["http"] = httpx.AsyncClient(timeout=30)
    yield
    await state["http"].aclose()
    await close(state["nc"])


app = FastAPI(title="blade gateway", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "nats": bool(state["nc"] and state["nc"].is_connected), "services": URLS}


@app.post("/sessions")
async def create_session(spec: SessionSpec):
    return orch.create(spec)


@app.get("/sessions")
async def list_sessions():
    return list(orch.sessions.values())


def _session(sid: str):
    s = orch.sessions.get(sid)
    if s is None:
        raise HTTPException(404, f"no session {sid}")
    return s


@app.get("/sessions/{sess_id}")
async def get_session(sess_id: str):
    return _session(sess_id)


@app.post("/sessions/{sess_id}/start")
async def start_session(sess_id: str):
    return await orch.start(_session(sess_id))


@app.post("/sessions/{sess_id}/stop")
async def stop_session(sess_id: str):
    return await orch.stop(_session(sess_id))


class ReplayPatch(BaseModel):
    paused: bool | None = None
    speed: float | None = None
    seek_s: float | None = None
    step: int | None = None


@app.patch("/sessions/{sess_id}/replays/{sid}")
async def patch_replay(sess_id: str, sid: str, body: ReplayPatch):
    s = _session(sess_id)
    if sid not in s.sids:
        raise HTTPException(404, f"{sid} is not in session {sess_id}")
    r = await state["http"].patch(f"{URLS['sessions']}/replays/{sid}", json=body.model_dump(exclude_none=True))
    return Response(r.content, r.status_code, media_type="application/json")


@app.get("/detectors")
async def detectors():
    live, assign = await orch.detectors()
    return {"live": live, "assign": assign}


@app.get("/streams")
async def streams():
    r = await state["http"].get(f"{URLS['ingest']}/streams")
    return Response(r.content, r.status_code, media_type="application/json")


@app.api_route("/api/{area}/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/api/{area}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(area: str, request: Request, rest: str = ""):
    if area not in PROXIES:
        raise HTTPException(404, f"no API {area}")
    svc, base = PROXIES[area]
    url = f"{URLS[svc]}{base}" + (f"/{rest}" if rest else "")
    headers = {k: v for k, v in request.headers.items() if k.lower() in ("content-type", "accept")}
    r = await state["http"].request(request.method, url, params=request.query_params, content=await request.body(),
                                    headers=headers)
    keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "x-layer", "x-extent")}
    return Response(r.content, r.status_code, headers=keep)


def _preview(jpeg: bytes, width: int) -> tuple[bytes, int, int]:
    im = Image.open(io.BytesIO(jpeg))
    if im.width > width:
        im = im.resize((width, round(im.height * width / im.width)), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=80)
    return buf.getvalue(), im.width, im.height


@app.websocket("/ws")
async def ws(websocket: WebSocket, session: str | None = None, sids: str = "", wids: str = ""):
    await websocket.accept()
    if session:
        s = orch.sessions.get(session)
        if s is None:
            await websocket.close(code=4404, reason=f"no session {session}")
            return
        sid_list, wid_list = s.sids, s.wids
        await websocket.send_text(json.dumps({"type": "session", "data": s.model_dump()}))
    else:
        sid_list = [x for x in sids.split(",") if x]
        wid_list = [x for x in wids.split(",") if x]
    if not sid_list and not wid_list:
        await websocket.close(code=4400, reason="give ?session= or ?sids=&wids=")
        return

    out: asyncio.Queue = asyncio.Queue(maxsize=256)
    last_preview: dict[str, float] = {}
    nc = state["nc"]
    subs = []

    def put(item) -> None:
        try:
            out.put_nowait(item)
        except asyncio.QueueFull:
            pass  # a slow client loses messages, never stalls the bus

    async def on_msg(msg):
        kind = msg.subject.split(".")[0]
        env, blob = decode(msg)
        if kind == "frames":
            t = time.time()
            if t - last_preview.get(env.sid, 0) < 1.0 / PREVIEW_FPS or out.qsize() > 64:
                return
            last_preview[env.sid] = t
            jpeg, w, h = await asyncio.to_thread(_preview, blob, PREVIEW_W)
            head = json.dumps({"type": "frame", "sid": env.sid, "frame": env.frame, "w": w, "h": h,
                               "src": env.data.get("tel", {}).get("image"), "ts": env.ts}).encode()
            put(b"ZPIF" + struct.pack(">I", len(head)) + head + jpeg)
            return
        typ = "world." + msg.subject.split(".")[-1] if kind == "world" else kind
        if typ == "detections":
            env.data.pop("tel", None)
        put(json.dumps({"type": typ, "sid": env.sid, "wid": env.wid, "frame": env.frame, "ts": env.ts,
                        "data": env.data}, separators=(",", ":")))

    for sid in sid_list:
        for subj in ("frames", "detections", "geo"):
            subs.append(await nc.subscribe(f"{subj}.{sid}", cb=on_msg))
    for wid in wid_list:
        subs.append(await nc.subscribe(f"world.{wid}.>", cb=on_msg))
        subs.append(await nc.subscribe(f"alerts.{wid}", cb=on_msg))
        subs.append(await nc.subscribe(f"eval.{wid}", cb=on_msg))
        try:
            r = await state["http"].get(f"{URLS['world']}/worlds/{wid}/snapshot")
            if r.status_code == 200:
                put(json.dumps({"type": "snapshot", "wid": wid, "data": r.json()}))
        except httpx.HTTPError:
            pass
    subs.append(await nc.subscribe("diag.>", cb=on_msg))

    async def sender():
        while True:
            item = await out.get()
            if isinstance(item, bytes):
                await websocket.send_bytes(item)
            else:
                await websocket.send_text(item)

    task = asyncio.create_task(sender())
    try:
        while True:  # only to notice the client leaving
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        for sub in subs:
            await sub.unsubscribe()


def main() -> None:
    run_app("blade_gateway.main:app", ports.GATEWAY)
