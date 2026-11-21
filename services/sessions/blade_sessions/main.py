"""sessions: record, store, import and replay streams (REST: /recordings, /recording/{sid}, /replays)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from pydantic import BaseModel

from blade_common import ports
from blade_common.bus import close, connect
from blade_common.service import env_bool, env_float, env_int, env_str, run_app
from blade_common.util import extract, zip_root

from .recorder import Recorder
from .recording import finalize, list_recordings
from .replay import Replay

log = logging.getLogger("sessions")

ROOT = Path(env_str("RECORDINGS_DIR", "recordings"))
INGEST_URL = env_str("INGEST_URL", f"http://127.0.0.1:{ports.INGEST}")
INGEST_TRUTH = (env_str("INGEST_TRUTH_HOST", "127.0.0.1"), env_int("INGEST_TRUTH_PORT", ports.TRUTH_UDP))

recorder = Recorder(ROOT, env_bool("RECORD_ALL", True), env_float("RECORD_IDLE_S", 5.0))
replays: dict[str, Replay] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    ROOT.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(recorder.recover)
    nc = await connect("sessions")
    recorder.js = nc.jetstream()
    await nc.subscribe("raw.>", cb=recorder.on_raw)
    for subj in ("truth.>", "registry.>", "events.>"):
        await nc.subscribe(subj, cb=recorder.on_truth)
    reaper = asyncio.create_task(recorder.reap())
    yield
    reaper.cancel()
    await recorder.close()
    for r in replays.values():
        await r.stop()
    await close(nc)


app = FastAPI(title="blade sessions", lifespan=lifespan)


def _rec_dir(rid: str) -> Path:
    if "/" in rid or "\\" in rid or rid.startswith(".") or not (ROOT / rid / "stream.ts").exists():
        raise HTTPException(404, f"no recording {rid}")
    return ROOT / rid


@app.get("/health")
async def health():
    return {"ok": True, "recordings": str(ROOT), "recording": len(recorder.active), "replays": len(replays)}


@app.get("/recordings")
async def recordings():
    return list_recordings(ROOT)


@app.get("/recordings/{rid}")
async def recording(rid: str):
    m = _rec_dir(rid) / "manifest.json"
    if not m.exists():
        raise HTTPException(404, f"{rid} has no manifest")
    return json.loads(m.read_text())


@app.post("/recordings")
async def upload(file: UploadFile, name: str | None = None):
    data = await file.read()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise HTTPException(400, "not a zip") from None
    prefix = zip_root(z, "stream.ts")
    if prefix is None:
        raise HTTPException(400, "zip has no stream.ts")
    rid = name or (prefix.strip("/").split("/")[-1] if prefix else Path(file.filename or "upload").stem)
    d = ROOT / rid
    if d.exists():
        raise HTTPException(409, f"{rid} exists")
    extract(z, prefix, d)
    return await asyncio.to_thread(finalize, d, {"id": rid})


class Register(BaseModel):
    path: str
    name: str | None = None


@app.post("/recordings/register")
async def register(body: Register):
    src = Path(body.path).expanduser().resolve()
    if not (src / "stream.ts").exists():
        raise HTTPException(400, f"{src} has no stream.ts")
    rid = body.name or src.name
    d = ROOT / rid
    if d.exists():
        raise HTTPException(409, f"{rid} exists")
    d.symlink_to(src, target_is_directory=True)
    return await asyncio.to_thread(finalize, d, {"id": rid})


@app.get("/recording/active")
async def active():
    return {"record_all": recorder.record_all, "overrides": recorder.wanted, "active": recorder.status()}


class RecordToggle(BaseModel):
    on: bool


@app.put("/recording/{sid}")
async def toggle(sid: str, body: RecordToggle):
    recorder.wanted[sid] = body.on
    closed = None if body.on else await recorder.stop(sid)
    return {"sid": sid, "on": body.on, "closed": closed}


class ReplayReq(BaseModel):
    recording: str
    sid: str
    wid: str | None = None
    speed: float = 1.0
    loop: bool = False
    offset_s: float = 0.0
    pacing: str = "realtime"
    delivery: str = "latest"
    truth: bool = True


class ReplayPatch(BaseModel):
    paused: bool | None = None
    speed: float | None = None
    seek_s: float | None = None
    step: int | None = None


@app.post("/replays")
async def start_replay(req: ReplayReq):
    if req.pacing not in ("realtime", "max"):
        raise HTTPException(400, "pacing: realtime | max")
    if req.delivery not in ("latest", "all"):
        raise HTTPException(400, "delivery: latest | all")
    old = replays.get(req.sid)
    if old and old.state in ("starting", "playing", "paused"):
        raise HTTPException(409, f"{req.sid} is already playing")
    try:
        r = Replay(sid=req.sid, recording=req.recording, dir=_rec_dir(req.recording), ingest_url=INGEST_URL,
                   truth_addr=INGEST_TRUTH, wid=req.wid, speed=req.speed, loop=req.loop, offset_s=req.offset_s,
                   pacing=req.pacing, delivery=req.delivery, send_truth=req.truth)
    except FileNotFoundError as e:
        raise HTTPException(400, f"{req.recording}: {e}") from e
    replays[req.sid] = r
    r.start()
    return r.info()


@app.get("/replays")
async def list_replays():
    return [r.info() for r in replays.values()]


@app.patch("/replays/{sid}")
async def patch_replay(sid: str, body: ReplayPatch):
    r = replays.get(sid) or _404(sid)
    r.patch(body.paused, body.speed, body.seek_s, body.step)
    return r.info()


@app.delete("/replays/{sid}")
async def stop_replay(sid: str):
    r = replays.pop(sid, None) or _404(sid)
    await r.stop()
    return r.info()


def _404(sid: str):
    raise HTTPException(404, f"no replay {sid}")


def main() -> None:
    run_app("blade_sessions.main:app", ports.SESSIONS)
