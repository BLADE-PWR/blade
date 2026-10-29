"""world-model: geo.<sid> -> world.<wid>.objects / .events (+ ack.<sid> for delivery "all"). Fusion is in
fusion.py; streams of different worlds never merge. Unseen objects stay "stale" for EXPIRE_S."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from blade_common import ports
from blade_common.bus import close, connect, decode, forward, jdump, now, publish
from blade_common.contracts import Envelope, WorldEventsData, WorldObjectsData
from blade_common.service import Diag, env_float, env_int, env_str, run_app

from .fusion import World

log = logging.getLogger("world")
STALE_S = env_float("STALE_S", 3.0)
EXPIRE_S = env_float("EXPIRE_S", 600.0)
EVENT_TTL_S = env_float("EVENT_TTL_S", 5.0)
CONFIRM_N = env_int("CONFIRM_N", 5)
EVENT_CLASSES = set(env_str("EVENT_CLASSES", "explosion,tank_fire").split(","))

worlds: dict[str, World] = {}
state: dict = {"nc": None}
diag = Diag("world")


def world(wid: str) -> World:
    if wid not in worlds:
        worlds[wid] = World(wid, STALE_S, EXPIRE_S, EVENT_TTL_S, EVENT_CLASSES, CONFIRM_N)
        log.info("new world %s", wid)
    return worlds[wid]


async def on_geo(msg) -> None:
    t_in = now()
    env, _ = decode(msg)
    wid = env.wid or "default"
    w = world(wid)
    changed, events = w.observe(env.sid, env.data.get("pts", []), t_in)
    merged = w.take_merged()
    diag.got(env.sid)
    if changed or merged:
        data = WorldObjectsData(objects=[o.model() for o in changed], expired=merged,
                                counts=w.smoothed_counts(t_in))
        out = forward(env, data.model_dump(exclude_none=True), "world", t_in)
        await publish(state["nc"], f"world.{wid}.objects", out)
        diag.sent(env.sid, latency_s=now() - env.ts.get("capture", now()))
    if events:
        data = WorldEventsData.model_validate({"events": events}).model_dump()
        await publish(state["nc"], f"world.{wid}.events", forward(env, data, "world", t_in))
    if env.delivery == "all" and env.frame is not None:
        await state["nc"].publish(f"ack.{env.sid}", jdump({"v": 1, "sid": env.sid, "frame": env.frame,
                                                           "data": {"frame": env.frame}}))


async def sweeper() -> None:
    while True:
        await asyncio.sleep(1.0)
        t = time.time()
        for wid, w in list(worlds.items()):
            stale, expired = w.sweep(t)
            if stale or expired:
                data = WorldObjectsData(objects=[o.model() for o in stale], expired=expired,
                                        counts=w.smoothed_counts(t))
                env = Envelope(wid=wid, ts={"world": [t, time.time()]}, data=data.model_dump(exclude_none=True))
                await publish(state["nc"], f"world.{wid}.objects", env)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["nc"] = await connect("world")
    await state["nc"].subscribe("geo.>", cb=on_geo)
    tasks = [asyncio.create_task(sweeper()), asyncio.create_task(diag.run(state["nc"]))]
    yield
    for t in tasks:
        t.cancel()
    await close(state["nc"])


app = FastAPI(title="blade world-model", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "worlds": {w: len(x.published()) for w, x in worlds.items()}}


@app.get("/worlds")
async def list_worlds():
    return [{"wid": w.wid, "objects": len(w.published()), "associations": dict(w.stats)} for w in worlds.values()]


@app.get("/worlds/{wid}/snapshot")
async def snapshot(wid: str):
    w = worlds.get(wid)
    objs = [o.model().model_dump(exclude_none=True) for o in w.published()] if w else []
    events = [{k: v for k, v in e.items() if k != "seen"} for e in w.events.values()] if w else []
    return {"wid": wid, "t": time.time(), "objects": objs, "events": events}


@app.get("/worlds/{wid}/objects/{oid}")
async def obj(wid: str, oid: str):
    w = worlds.get(wid)
    o = w.objects.get(oid) if w else None
    if o is None or not o.confirmed:
        raise HTTPException(404, f"{wid}/{oid} not found")
    return {**o.model().model_dump(exclude_none=True), "history": list(o.history)}


def main() -> None:
    run_app("blade_world.main:app", ports.WORLD)
