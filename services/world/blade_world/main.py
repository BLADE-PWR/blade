"""world-model: geo.<sid> -> world.<wid>.objects / .events (+ ack.<sid> for delivery "all"). Fusion is in
fusion.py; streams of different worlds never merge. Unseen objects stay "stale" for EXPIRE_S. Each world runs on
its own clock (clock.py): the wall clock live, the recording's time in a replay, published as WorldObjectsData.t.
A world is on one map, set by the first drone that reports into it; drones on another map are left out (map
coordinates of different maps overlap). Once no drone on its map has reported for MAP_IDLE_S, the next drone on
another map moves the world there, and it starts afresh: its old objects are published as forgotten."""
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

from .clock import WorldClock
from .fusion import World

log = logging.getLogger("world")
STALE_S = env_float("STALE_S", 3.0)
EXPIRE_S = env_float("EXPIRE_S", 600.0)
EVENT_TTL_S = env_float("EVENT_TTL_S", 5.0)
CONFIRM_N = env_int("CONFIRM_N", 5)
EVENT_CLASSES = set(env_str("EVENT_CLASSES", "explosion,tank_fire").split(","))
MAP_IDLE_S = env_float("MAP_IDLE_S", 30.0)

worlds: dict[str, World] = {}
clocks: dict[str, WorldClock] = {}
swept: dict[str, float] = {}  # wid -> world time at the last sweep
maps: dict[str, str] = {}  # wid -> map
map_seen: dict[str, float] = {}  # wid -> wall time of the last frame on its map
left_out: set[tuple[str, str]] = set()  # (wid, sid) logged as being on another map
state: dict = {"nc": None}
diag = Diag("world")


def world(wid: str) -> World:
    if wid not in worlds:
        worlds[wid] = World(wid, STALE_S, EXPIRE_S, EVENT_TTL_S, EVENT_CLASSES, CONFIRM_N)
        clocks[wid] = WorldClock(time.time(), EXPIRE_S)
        log.info("new world %s", wid)
    return worlds[wid]


async def on_map(wid: str, sid: str, name: str, wall: float) -> bool:
    """-> whether this drone's frame belongs on the world's map."""
    cur = maps.get(wid)
    if cur is not None and cur != name and wall - map_seen[wid] > MAP_IDLE_S:
        log.info("world %s: no drone on %s for %.0f s, moving to %s", wid, cur, wall - map_seen[wid], name)
        maps[wid] = cur = name  # before the await in reset: the sweeper must not publish the old map
        await reset(wid, name)
    if cur is None:
        maps[wid] = cur = name
    if cur != name:
        if (wid, sid) not in left_out:
            left_out.add((wid, sid))
            log.warning("%s is on map %s, but world %s is on %s: its detections are left out", sid, name, wid, cur)
        return False
    left_out.discard((wid, sid))
    map_seen[wid] = wall
    return True


async def reset(wid: str, name: str) -> None:
    """The world starts afresh on another map; clients drop its old objects."""
    old = worlds.pop(wid)
    del clocks[wid]
    swept.pop(wid, None)
    world(wid)
    data = WorldObjectsData(forgotten=[o.oid for o in old.published()], t=clocks[wid].t, map=name)
    await publish(state["nc"], f"world.{wid}.objects", Envelope(wid=wid, data=data.model_dump(exclude_none=True)))


async def on_geo(msg) -> None:
    t_in = now()
    env, _ = decode(msg)
    wid = env.wid
    if not await on_map(wid, env.sid, env.data["map"], time.time()):
        await ack(env)
        return
    w = world(wid)
    t, rewind = clocks[wid].frame(env.sid, env.ts.get("media"), t_in)
    forgotten = w.forget(env.sid) if rewind else []
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
    await ack(env)


async def ack(env: Envelope) -> None:
    if env.delivery == "all" and env.frame is not None:
        await state["nc"].publish(f"ack.{env.sid}", jdump({"v": 1, "sid": env.sid, "frame": env.frame,
                                                           "data": {"frame": env.frame}}))


async def sweep_once(wall: float) -> None:
    """Age every world on its own clock; publish what went stale or expired, and the clock whenever it moved"""
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
