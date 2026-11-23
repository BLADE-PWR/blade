"""world-model: geo.<sid> -> world.<wid>.objects / .events. Fusion is in
fusion.py; streams of different worlds never merge. Unseen objects stay "stale" for EXPIRE_S. Each world runs on
its own clock (clock.py): the wall clock live, the recording's time in a replay, published as WorldObjectsData.t.
A world is on one map for its whole life: ingest gives drones on another map another world. When ingest ends a
world (worlds.<wid> with ended), it is dropped."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from blade_common import ports
from blade_common.bus import close, connect, decode, forward, now, publish
from blade_common.contracts import Envelope, WorldEventsData, WorldObjectsData
from blade_common.service import Diag, env_float, env_int, env_str, run_app

from .clock import WorldClock
from .fusion import World

log = logging.getLogger("world")
STALE_S = env_float("STALE_S", 3.0)
EXPIRE_S = env_float("EXPIRE_S", 600.0)
EVENT_TTL_S = env_float("EVENT_TTL_S", 5.0)
CONFIRM_N = env_int("CONFIRM_N", 5)
EVENT_CLASSES = set(env_str("EVENT_CLASSES", "explosion,tank_fire").split(","))

worlds: dict[str, World] = {}
clocks: dict[str, WorldClock] = {}
swept: dict[str, float] = {}  # wid -> world time at the last sweep
maps: dict[str, str] = {}  # wid -> map
ended: set[str] = set()  # worlds ingest ended: geo still on its way for them is dropped, not a new world
state: dict = {"nc": None}
diag = Diag("world")


def world(wid: str) -> World:
    if wid not in worlds:
        worlds[wid] = World(wid, STALE_S, EXPIRE_S, EVENT_TTL_S, EVENT_CLASSES, CONFIRM_N)
        clocks[wid] = WorldClock(time.time(), EXPIRE_S)
        log.info("new world %s", wid)
    return worlds[wid]


async def on_world_info(msg) -> None:
    env, _ = decode(msg)
    wid = env.data["wid"]
    if not env.data.get("ended"):
        ended.discard(wid)  # a replay world comes back under its id when its session starts again
        return
    ended.add(wid)
    w = worlds.pop(wid, None)
    for d in (clocks, swept, maps):
        d.pop(wid, None)
    if w is not None:
        log.info("world %s ended: dropped with %d objects", wid, len(w.objects))


async def on_geo(msg) -> None:
    t_in = now()
    env, _ = decode(msg)
    wid = env.wid
    if wid in ended:
        return
    w = world(wid)
    maps.setdefault(wid, env.data["map"])
    t, rewind = clocks[wid].frame(env.sid, env.ts.get("media"), t_in)
    forgotten = []
    if rewind:  # the replay went back (a seek, a loop): every drone of it did, also ones that send nothing there
        forgotten = w.forget_all()
        clocks[wid].rejoin_all(env.sid)
    changed, events = w.observe(env.sid, env.data.get("pts", []), t)
    forgotten += w.take_merged()
    diag.got(env.sid)
    if changed or forgotten:
        data = WorldObjectsData(objects=[o.model() for o in changed], forgotten=forgotten,
                                counts=w.smoothed_counts(t), t=clocks[wid].t, map=maps[wid])
        out = forward(env, data.model_dump(exclude_none=True), "world", t_in)
        await publish(state["nc"], f"world.{wid}.objects", out)
        diag.sent(env.sid, latency_s=now() - env.ts.get("capture", now()))
    if events:
        data = WorldEventsData.model_validate({"events": events}).model_dump()
        await publish(state["nc"], f"world.{wid}.events", forward(env, data, "world", t_in))


async def sweep_once(wall: float) -> None:
    for wid, w in list(worlds.items()):
        t = clocks[wid].tick(wall)
        stale, expired = w.sweep(t)
        if stale or expired or swept.get(wid) != t:
            data = WorldObjectsData(objects=[o.model() for o in stale], expired=expired,
                                    counts=w.smoothed_counts(t), t=t, map=maps[wid])
            env = Envelope(wid=wid, ts={"world": [wall, time.time()]}, data=data.model_dump(exclude_none=True))
            await publish(state["nc"], f"world.{wid}.objects", env)
        swept[wid] = t


async def sweeper() -> None:
    while True:
        await asyncio.sleep(1.0)
        await sweep_once(time.time())


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["nc"] = await connect("world")
    await state["nc"].subscribe("geo.>", cb=on_geo)
    await state["nc"].subscribe("worlds.*", cb=on_world_info)
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
    return [{"wid": w.wid, "map": maps.get(w.wid), "objects": len(w.published()), "associations": dict(w.stats)}
            for w in worlds.values()]


@app.get("/worlds/{wid}/snapshot")
async def snapshot(wid: str):
    w = worlds.get(wid)
    objs = [o.model().model_dump(exclude_none=True) for o in w.published()] if w else []
    events = [{k: v for k, v in e.items() if k != "seen"} for e in w.events.values()] if w else []
    return {"wid": wid, "t": clocks[wid].t if w else time.time(), "map": maps.get(wid), "objects": objs,
            "events": events}


@app.get("/worlds/{wid}/objects/{oid}")
async def obj(wid: str, oid: str):
    w = worlds.get(wid)
    o = w.objects.get(oid) if w else None
    if o is None or not o.confirmed:
        raise HTTPException(404, f"{wid}/{oid} not found")
    return {**o.model().model_dump(exclude_none=True), "history": list(o.history)}


def main() -> None:
    run_app("blade_world.main:app", ports.WORLD)
