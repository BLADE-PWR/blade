"""detector: frames.<sid> -> detections.<sid>. ONNX Runtime YOLO, one BoT-SORT per stream.
One slot per stream (newest frame wins); waiting frames of several streams form one batch.
MODEL=oracle uses sim truth instead (oracle.py). MODEL=auto (the default) picks the model per frame from the scale
of the scene (routing.py); a stream gets empty detections (model "none") while no model covers it."""
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
from .routing import Entry, Router, Routing, describe, scale_range
from .tracking import StreamTracker

log = logging.getLogger("detector")

MODELS_DIR = Path(env_str("MODELS_DIR", "models"))
DEFAULT_MODEL = env_str("MODEL", "auto")
# the models auto mode chooses from until the operator sets them
AUTO_MODELS = [m for m in env_str("AUTO_MODELS", "arma_detector_v2_auto@2").split(",") if m]
MAX_BATCH = env_int("MAX_BATCH", 8)
SUBJECTS = env_str("STREAMS", "frames.>")
MAX_FPS = env_float("MAX_FPS", 0.0)  # live streams: at most this many frames/s per stream (0 = all)
ORACLE_IDS = ("oracle", Oracle.id)
AUTO = "auto"
NO_MODEL = "none"  # what an auto stream runs while no model covers its scale


def default_routing() -> Routing:
    return Routing([Entry(m) for m in AUTO_MODELS])


class Detector:
    def __init__(self):
        self.packages: dict[str, ModelPackage] = {}
        self.trackers: dict[tuple[str, str], StreamTracker] = {}
        self.top_tid: dict[str, int] = {}  # sid -> highest track id sent, where its next tracker continues
        self.slots: dict[str, tuple] = {}  # sid -> (env, jpeg, t_in), newest wins
        self.ready = asyncio.Event()
        self.oracle = Oracle()
        self.diag = Diag("detector")
        self.nc = None
        self.cluster = Cluster()
        self.router = Router()
        self._routing: tuple[dict | None, Routing] = (None, default_routing())
        self.last_frame: dict[str, float] = {}
        self.accepted: dict[str, float] = {}  # sid -> when the last live frame was taken

    def model_for(self, sid: str) -> str:
        return self.cluster.model(sid) or DEFAULT_MODEL

    def routing(self) -> Routing:
        """The operator's auto routing (KV, refreshed by the cluster), else the one from AUTO_MODELS."""
        raw = self.cluster.routing
        if raw != self._routing[0]:
            try:
                self._routing = (raw, Routing.from_dict(raw) if raw else default_routing())
            except (KeyError, TypeError, ValueError):
                log.exception("bad routing in KV: keeping the previous one")
                self._routing = (raw, self._routing[1])
        return self._routing[1]

    def resolve(self, sid: str, env, t: float) -> str:
        """The model that runs this frame: the stream's own, or in auto mode what the scale of the scene picks."""
        mid = self.model_for(sid)
        if mid != AUTO:
            return mid
        tel = env.data["tel"]
        size = env.data.get("jpeg") or tel["image"]
        before = self.router.current(sid)
        pick = self.router.choose(self.routing(), self.packages, sid, tel, size["w"], size["h"], t)
        if before is not None and before.model != pick.model:
            log.info("%s: auto model %s -> %s", sid, before.model, pick.model)
            for key in [k for k in self.trackers if k[0] == sid]:
                del self.trackers[key]  # its ids go on from top_tid; stale tracks must not match new objects
        return pick.model or NO_MODEL

    def use_loaded_for_auto(self) -> None:
        """When none of AUTO_MODELS is loaded, auto runs the first model there is: better than empty frames."""
        if self.packages and not any(m in self.packages for m in AUTO_MODELS):
            AUTO_MODELS[:] = list(self.packages)[:1]
            self._routing = (None, default_routing())
            log.error("no AUTO_MODELS package found in %s: auto uses %s", MODELS_DIR, AUTO_MODELS[0])

    def first_model(self) -> str:
        """The first loaded model of the auto routing: what auto starts a stream with."""
        return next((e.model for e in self.routing().models if e.model in self.packages), NO_MODEL)

    def active_model(self, sid: str) -> str:
        """The model running the stream now (the one auto last picked, if the stream is in auto mode)."""
        mid = self.model_for(sid)
        if mid != AUTO:
            return mid
        c = self.router.current(sid)
        return (c.model or NO_MODEL) if c else self.first_model()

    def conf_for(self, sid: str) -> float:
        """The stream's box confidence threshold; by default the model's tracker threshold (conf.track)."""
        conf, pkg = self.cluster.conf(sid), self.packages.get(self.active_model(sid))
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
        if MAX_FPS > 0 and t - self.accepted.get(sid, 0.0) < 1.0 / MAX_FPS:
            self.diag.drop(sid)
            return
        self.accepted[sid] = t
        if sid in self.slots:
            self.diag.drop(sid)
        self.slots[sid] = (env, jpeg, t)
        self.ready.set()

    async def on_world_info(self, msg) -> None:
        """A drone that had another stream id before (it reconnected after its world ended, or changed maps) keeps
        its model and threshold; the streams of an ended world are forgotten."""
        env, _ = decode(msg)
        w = env.data
        for sid, old in (w.get("previous") or {}).items():
            await self.cluster.move(old, sid, ("model", "conf"))
        if w.get("ended"):
            for sid in w.get("drones") or {}:
                for key in [k for k in self.trackers if k[0] == sid]:
                    del self.trackers[key]
                for d in (self.top_tid, self.slots, self.last_frame, self.accepted):
                    d.pop(sid, None)
                self.router.forget(sid)

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
            by_model: dict[str, list] = {}
            for sid, item in work.items():
                by_model.setdefault(self.resolve(sid, item[0], item[2]), []).append((sid, *item))
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
        if model_id == NO_MODEL:  # no model covers the scale: say so with a frame that has no boxes
            for sid, env, _jpeg, t_in in items:
                tel = env.data["tel"]
                data = DetectionsData(model=NO_MODEL, img=(tel["image"]["w"], tel["image"]["h"]), dets=[], tel=tel)
                out.append(forward(env, data.model_dump(exclude_none=True), "detector", t_in))
                self.diag.skip(sid)
            return out
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
    if DEFAULT_MODEL == AUTO:
        svc.use_loaded_for_auto()
    for m in AUTO_MODELS if DEFAULT_MODEL == AUTO else [DEFAULT_MODEL]:
        if m in svc.packages:
            await asyncio.to_thread(svc.packages[m].session)
        elif m not in ORACLE_IDS:
            log.warning("model %s not found in %s", m, MODELS_DIR)
    svc.nc = await connect("detector")
    await svc.cluster.connect(svc.nc)
    await svc.nc.subscribe(SUBJECTS, cb=svc.on_frame)
    await svc.nc.subscribe("truth.>", cb=svc.on_truth)
    await svc.nc.subscribe("registry.>", cb=svc.on_truth)
    await svc.nc.subscribe("worlds.*", cb=svc.on_world_info)
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
            "streams": {sid: svc.active_model(sid) for sid in {k[0] for k in svc.trackers}}}


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
    out = {"sid": sid, "model": svc.model_for(sid)}
    c = svc.router.current(sid) if out["model"] == AUTO else None  # only the replica that runs the stream knows
    if c is not None:
        out |= {"active": c.model, "px": None if c.px is None else round(c.px, 1)}
    return out


@app.put("/streams/{sid}/model")
async def set_model(sid: str, body: StreamModel):
    if body.model not in svc.packages and body.model not in (*ORACLE_IDS, AUTO):
        _404(body.model)
    await svc.cluster.set(sid, model=body.model, conf=None)  # a threshold belongs to its model
    svc.router.forget(sid)
    for key in [k for k in svc.trackers if k[0] == sid]:
        del svc.trackers[key]
    return {"sid": sid, "model": body.model}


class RoutingEntry(BaseModel):
    model: str
    min_px: float | None = Field(None, ge=0, description="overrides scale.min_px of model.yaml")
    max_px: float | None = Field(None, ge=0, description="overrides scale.max_px of model.yaml")


class RoutingBody(BaseModel):
    models: list[RoutingEntry]
    margin: float = Field(0.15, ge=0.0, lt=1.0)
    hold_s: float = Field(1.0, ge=0.0)


@app.get("/routing")
async def get_routing():
    return describe(svc.routing(), svc.packages)


@app.put("/routing")
async def set_routing(body: RoutingBody):
    """The models the auto mode chooses from, in priority order, with the ranges (min_px, max_px) it uses."""
    try:
        routing = Routing.from_dict(body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    for e in routing.models:
        if e.model not in svc.packages:
            _404(e.model)
        _, lo, hi = scale_range(svc.packages[e.model], e)  # one limit alone can cross the other from model.yaml
        if lo >= hi:
            raise HTTPException(400, f"{e.model}: min_px {lo:g} must be below max_px {hi:g} (empty = model.yaml)")
    await svc.cluster.set_routing(routing.to_dict())
    return describe(routing, svc.packages)


@app.delete("/routing")
async def reset_routing():
    """Back to AUTO_MODELS and the ranges of model.yaml."""
    await svc.cluster.set_routing(None)
    return describe(svc.routing(), svc.packages)


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
    mid = model or (svc.first_model() if DEFAULT_MODEL == AUTO else DEFAULT_MODEL)
    pkg = svc.packages.get(mid) or _404(mid)
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
