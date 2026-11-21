"""detector: frames.<sid> -> detections.<sid>. ONNX Runtime YOLO, one BoT-SORT per stream.
One slot per stream (newest frame wins); waiting frames of several streams form one batch.
MODEL=oracle uses sim truth instead (oracle.py)."""
from __future__ import annotations

import asyncio
import io
import logging
import shutil
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile
from pydantic import BaseModel, Field

from blade_common import ports
from blade_common.bus import close, connect, decode, forward, now, publish
from blade_common.contracts import DetectionsData
from blade_common.service import Diag, env_float, env_int, env_str, run_app
from blade_common.util import extract, zip_root

from .cluster import Cluster
from .ground import auto_imgsz, too_big
from .oracle import Oracle
from .package import ModelPackage, detect_batch, find_packages
from .tracking import StreamTracker

log = logging.getLogger("detector")

MODELS_DIR = Path(env_str("MODELS_DIR", "models"))
DEFAULT_MODEL = env_str("MODEL", "arma_detector_v1@1")
MAX_BATCH = env_int("MAX_BATCH", 8)
SUBJECTS = env_str("STREAMS", "frames.>")
MAX_FPS = env_float("MAX_FPS", 0.0)  # live streams: at most this many frames/s per stream (0 = all)
ORACLE_IDS = ("oracle", Oracle.id)


class Detector:
    def __init__(self):
        self.packages: dict[str, ModelPackage] = {}
        self.trackers: dict[tuple[str, str], StreamTracker] = {}
        self.top_tid: dict[str, int] = {}  # sid -> highest track id sent, where its next tracker continues
        self.slots: dict[str, tuple] = {}  # sid -> (env, jpeg, t_in), newest wins
        self.fifo: dict[str, list] = {}  # sid -> frames of delivery "all" streams, in order
        self.ready = asyncio.Event()
        self.oracle = Oracle()
        self.diag = Diag("detector")
        self.nc = None
        self.cluster = Cluster()
        self.last_frame: dict[str, float] = {}
        self.accepted: dict[str, float] = {}  # sid -> when the last live frame was taken

    def model_for(self, sid: str) -> str:
        return self.cluster.model(sid) or DEFAULT_MODEL

    def conf_for(self, sid: str) -> float:
        """The stream's box confidence threshold; by default the model's tracker threshold (conf.track)."""
        conf, pkg = self.cluster.conf(sid), self.packages.get(self.model_for(sid))
        return conf if conf is not None else pkg.conf_track if pkg else 0.0

    def status(self) -> dict:
        t = now()
        streams = sorted(s for s, ts in self.last_frame.items() if t - ts < 5)
        return {"models": list(self.packages) + [Oracle.id], "streams": streams}

    def tracker(self, sid: str, pkg: ModelPackage) -> StreamTracker:
        key = (sid, pkg.id)
        if key not in self.trackers:
            self.trackers[key] = StreamTracker(pkg.tracker_args, self.top_tid.get(sid, 0))
        return self.trackers[key]

    async def on_frame(self, msg) -> None:
        env, jpeg = decode(msg)
        sid = env.sid
        if not self.cluster.mine(sid):
            return
        t = now()
        self.diag.got(sid)
        self.last_frame[sid] = t
        if env.delivery == "all":
            self.fifo.setdefault(sid, []).append((env, jpeg, t))
        else:
            if MAX_FPS > 0 and t - self.accepted.get(sid, 0.0) < 1.0 / MAX_FPS:
                self.diag.drop(sid)
                return
            self.accepted[sid] = t
            if sid in self.slots:
                self.diag.drop(sid)
            self.slots[sid] = (env, jpeg, t)
        self.ready.set()

    async def on_truth(self, msg) -> None:
        env, _ = decode(msg)
        if msg.subject.startswith("registry."):
            self.oracle.on_spawn(env.sid, env.data)
        else:
            self.oracle.on_gt(env.sid, env.data)

    async def run(self) -> None:
        while True:
            await self.ready.wait()
            self.ready.clear()
            work, self.slots = self.slots, {}
            for sid, q in list(self.fifo.items()):
                if q and sid not in work:
                    work[sid] = q.pop(0)
                if q:
                    self.ready.set()  # more queued: come round again
            by_model: dict[str, list] = {}
            for sid, item in work.items():
                by_model.setdefault(self.model_for(sid), []).append((sid, *item))
            for model_id, items in by_model.items():
                for k in range(0, len(items), MAX_BATCH):
                    try:
                        results = await asyncio.to_thread(self.process, model_id, items[k:k + MAX_BATCH])
                    except Exception:
                        log.exception("%s: batch failed", model_id)
                        continue
                    for env in results:
                        await publish(self.nc, f"detections.{env.sid}", env)
                        self.diag.sent(env.sid, latency_s=now() - env.ts.get("capture", now()))
            for sid in work:
                self.diag.queue(sid, int(sid in self.slots))

    def process(self, model_id: str, items: list) -> list:
        out = []
        if model_id in ORACLE_IDS:
            for sid, env, _jpeg, t_in in items:
                tel = env.data["tel"]
                dets = self.oracle.detect(sid, tel)
                data = DetectionsData(model=Oracle.id, img=(tel["image"]["w"], tel["image"]["h"]),
                                      dets=dets, tel=tel)
                out.append(forward(env, data.model_dump(exclude_none=True), "detector", t_in))
            return out
        pkg = self.packages.get(model_id)
        if pkg is None:
            raise RuntimeError(f"no model package {model_id} in {MODELS_DIR}")
        imgs = [cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR) for _, _, jpeg, _ in items]
        groups: dict[tuple, list[int]] = {}
        for i, (im, (_, env, _, _)) in enumerate(zip(imgs, items)):
            size = auto_imgsz(pkg.imgsz, pkg.auto_imgsz, env.data["tel"], im.shape[1], im.shape[0])
            groups.setdefault((im.shape, size), []).append(i)
        results: list = [None] * len(items)
        for (_, size), idx in groups.items():
            for i, r in zip(idx, detect_batch(pkg, [imgs[i] for i in idx], pkg.conf_track, imgsz=size)):
                results[i] = r
        for (sid, env, _jpeg, t_in), img, (xyxy, conf, cls) in zip(items, imgs, results):
            tel = env.data["tel"]
            W, H = tel["image"]["w"], tel["image"]["h"]
            sx, sy = W / img.shape[1], H / img.shape[0]
            keep = ~too_big(pkg.max_m, tel, xyxy, conf, cls, sx, sy)
            xyxy, conf, cls = xyxy[keep], conf[keep], cls[keep]
            rows = self.tracker(sid, pkg).update(xyxy, conf, cls, img)
            if len(rows):
                self.top_tid[sid] = max(self.top_tid.get(sid, 0), int(rows[:, 4].max()))
            dets, used, floor = [], set(), self.conf_for(sid)
            for x1, y1, x2, y2, tid, cf, c, di in rows:
                used.add(int(di))
                if cf >= floor:  # weaker tracked boxes still keep the track alive inside the tracker
                    dets.append(_det(pkg, int(c), float(cf), (x1, y1, x2, y2), sx, sy, int(tid)))
            for i in range(len(conf)):
                if i not in used and conf[i] >= max(floor, pkg.conf_detect):
                    dets.append(_det(pkg, int(cls[i]), float(conf[i]), xyxy[i], sx, sy, None))
            data = DetectionsData(model=pkg.id, img=(W, H), dets=dets, tel=tel)
            out.append(forward(env, data.model_dump(exclude_none=True), "detector", t_in))
        return out


def _det(pkg: ModelPackage, c: int, conf: float, box, sx: float, sy: float, tid: int | None) -> dict:
    x1, y1, x2, y2 = box
    return {"tid": tid, "cls": pkg.classes[c], "grp": pkg.group_of[c], "conf": round(conf, 3),
            "box": [round(float(x1) * sx, 1), round(float(y1) * sy, 1), round(float(x2) * sx, 1),
                    round(float(y2) * sy, 1)]}


svc = Detector()


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc.packages = find_packages(MODELS_DIR)
    log.info("model packages: %s; default %s", list(svc.packages) or "none", DEFAULT_MODEL)
    if DEFAULT_MODEL in svc.packages:
        await asyncio.to_thread(svc.packages[DEFAULT_MODEL].session)
    elif DEFAULT_MODEL not in ORACLE_IDS:
        log.warning("default model %s not found in %s", DEFAULT_MODEL, MODELS_DIR)
    svc.nc = await connect("detector")
    await svc.cluster.connect(svc.nc)
    await svc.nc.subscribe(SUBJECTS, cb=svc.on_frame)
    await svc.nc.subscribe("truth.>", cb=svc.on_truth)
    await svc.nc.subscribe("registry.>", cb=svc.on_truth)
    tasks = [asyncio.create_task(svc.run()), asyncio.create_task(svc.diag.run(svc.nc)),
             asyncio.create_task(svc.cluster.run(svc.status))]
    yield
    for t in tasks:
        t.cancel()
    await close(svc.nc)


app = FastAPI(title="blade detector", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "default_model": DEFAULT_MODEL, "models": list(svc.packages),
            "streams": {sid: svc.model_for(sid) for sid in {k[0] for k in svc.trackers}}}


@app.get("/models")
async def models():
    return [p.info() for p in svc.packages.values()] + [{"id": Oracle.id, "name": "oracle", "classes": []}]


@app.get("/models/{mid}")
async def model(mid: str):
    p = svc.packages.get(mid) or _404(mid)
    return p.info()


@app.post("/models")
async def upload_model(file: UploadFile):
    z = zipfile.ZipFile(io.BytesIO(await file.read()))
    prefix = zip_root(z, "model.yaml")
    if prefix is None:
        raise HTTPException(400, "zip has no model.yaml")
    tmp = MODELS_DIR / f".upload-{time.time_ns()}"
    extract(z, prefix, tmp)
    try:
        pkg = ModelPackage.load(tmp)
    except Exception as e:
        shutil.rmtree(tmp)
        raise HTTPException(400, f"bad package: {e}") from e
    dest = MODELS_DIR / f"{pkg.name}_v{pkg.version}"
    if dest.exists() or pkg.id in svc.packages:
        shutil.rmtree(tmp)
        raise HTTPException(409, f"{pkg.id} exists")
    tmp.rename(dest)
    svc.packages[pkg.id] = ModelPackage.load(dest)
    return svc.packages[pkg.id].info()


class StreamModel(BaseModel):
    model: str


@app.get("/streams/{sid}/model")
async def get_model(sid: str):
    return {"sid": sid, "model": svc.model_for(sid)}


@app.put("/streams/{sid}/model")
async def set_model(sid: str, body: StreamModel):
    if body.model not in svc.packages and body.model not in ORACLE_IDS:
        _404(body.model)
    await svc.cluster.set(sid, model=body.model, conf=None)  # a threshold belongs to its model
    for key in [k for k in svc.trackers if k[0] == sid]:
        del svc.trackers[key]
    return {"sid": sid, "model": body.model}


class StreamConf(BaseModel):
    conf: float = Field(ge=0.0, le=1.0)


@app.get("/streams/{sid}/conf")
async def get_conf(sid: str):
    return {"sid": sid, "conf": svc.conf_for(sid)}


@app.put("/streams/{sid}/conf")
async def set_conf(sid: str, body: StreamConf):
    await svc.cluster.set(sid, conf=body.conf)
    return {"sid": sid, "conf": body.conf}


@app.get("/cluster")
async def cluster():
    c = svc.cluster
    return {"me": c.me, "live": c.live, "assign": c.assign,
            "owners": {sid: c.owner(sid) for sid in set(c.assign) | set(svc.last_frame)}}


@app.post("/detect")
async def detect_one(file: UploadFile, model: str | None = None, conf: float | None = None):
    pkg = svc.packages.get(model or DEFAULT_MODEL) or _404(model or DEFAULT_MODEL)
    img = cv2.imdecode(np.frombuffer(await file.read(), np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "not an image")
    (xyxy, cf, cl), = await asyncio.to_thread(detect_batch, pkg, [img], conf or pkg.conf_detect)
    return {"model": pkg.id, "img": [img.shape[1], img.shape[0]],
            "dets": [_det(pkg, int(c), float(s), b, 1.0, 1.0, None) for b, s, c in zip(xyxy, cf, cl)]}


def _404(what):
    raise HTTPException(404, f"not found: {what}")


def main() -> None:
    run_app("blade_detector.main:app", ports.DETECTOR)
