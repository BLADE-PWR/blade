"""decision-support: world.<wid>.objects / .events + geo.<sid> -> alerts.<wid> (rules.py), published when
an alert appears and when it clears, and clusters.<wid> (clusters.py), published every second.
Rules run on the world's clock (WorldObjectsData.t), so a paused replay raises
no lost contact; a world that sends no clock runs on the wall clock."""
from __future__ import annotations

import asyncio
import copy
import itertools
import logging
import time
from collections import deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from blade_common import ports
from blade_common.bus import close, connect, decode, publish
from blade_common.contracts import Alert, AlertsData, ClustersData, Envelope
from blade_common.service import env_float, run_app

from .clusters import Grouper, Params, same_group
from .rules import DEFAULTS, MEMBER_KEYED, RULES, WorldState

log = logging.getLogger("decision")
PERIOD_S = env_float("PERIOD_S", 1.0)
CLEAR_AFTER_S = env_float("CLEAR_AFTER_S", 5.0)
GONE_KEEP_S = env_float("GONE_KEEP_S", 600.0)
CLUSTER_PARAMS = Params(env_float("CLUSTER_RADIUS_M", Params.radius_m),
                        int(env_float("CLUSTER_MIN_COUNT", Params.min_count)),
                        env_float("CLUSTER_MAX_AGE_S", Params.max_age_s))


class Engine:
    def __init__(self, wid: str):
        self.wid = wid
        self.ws = WorldState()
        self.rules = copy.deepcopy(DEFAULTS)
        self.active: dict[str, Alert] = {}
        self.history: deque[Alert] = deque(maxlen=200)
        self.missing: dict[str, float] = {}  # alert key -> when its condition became false
        self.clock: float | None = None  # the world's time, s, from its object updates
        self.map: str | None = None  # the world's map, from its object updates
        self.grouper = Grouper(CLUSTER_PARAMS)
        self.had_clusters = False  # the last publication had groups: an empty one must follow when they are gone
        self._ids = itertools.count(1)

    def now(self) -> float:
        return time.time() if self.clock is None else self.clock

    def clear_now(self, keys: list[str]) -> list[Alert]:
        """Clear these alerts at once, without waiting CLEAR_AFTER_S."""
        out = []
        for key in keys:
            a = self.active.pop(key)
            self.missing.pop(key, None)
            a.state, a.t = "cleared", self.now()
            out.append(a)
        return out

    def track_groups(self, t: float) -> None:
        """The force groups now, and the velocity of each member over the last window of the group_motion rule."""
        self.ws.groups = self.grouper.update(self.ws.objects, t)
        keep = self.rules["group_motion"]["window_s"] + 5.0
        motion = {}
        for g in self.ws.groups:
            hist = self.ws.motion.get(g["cid"], deque())
            members = [self.ws.objects[oid] for oid in g["objects"] if oid in self.ws.objects]
            vels = {o["oid"]: o["vel"] for o in members if o.get("vel") and o["state"] == "active"}
            if vels and (not hist or t > hist[-1][0]):  # a paused world clock adds nothing
                hist.append((t, vels))
            while hist and t - hist[0][0] > keep:
                hist.popleft()
            motion[g["cid"]] = hist
        self.ws.motion = motion

    def step(self, t: float) -> list[Alert]:
        """Run every enabled rule; -> alerts raised or cleared now."""
        self.ws.events = [e for e in self.ws.events if t - e["t"] < 30]
        self.ws.gone = {k: v for k, v in self.ws.gone.items() if t - v["last"] < GONE_KEEP_S}
        try:
            self.track_groups(t)
        except Exception:
            log.exception("force groups")
        cues = {}
        for name, fn in RULES.items():
            p = self.rules.get(name, {})
            if p.get("enabled", True):
                try:
                    for c in fn(self.ws, p, t):
                        cues[c.key] = c
                except Exception:
                    log.exception("rule %s", name)
        # a group whose members changed a little is the same group: carry its alert over to the new key
        for key, c in list(cues.items()):
            if key in self.active or not c.objects or c.rule not in MEMBER_KEYED:
                continue
            for old_key, a in list(self.active.items()):
                if a.rule == c.rule and old_key not in cues and same_group(set(a.objects), set(c.objects)):
                    self.active[key] = self.active.pop(old_key)
                    break
        changed = []
        for key, c in cues.items():
            a = self.active.get(key)
            if a is None:
                a = Alert(aid=f"a-{next(self._ids):05d}", rule=c.rule, severity=c.severity, title=c.title,
                          explanation=c.explanation, objects=c.objects, confidence=c.confidence, xy=c.xy, t=t)
                self.active[key] = a
                self.history.append(a)
                changed.append(a)
            else:
                a.explanation, a.objects, a.confidence, a.xy = c.explanation, c.objects, c.confidence, c.xy
        for key in cues:
            self.missing.pop(key, None)
        for key in [k for k in self.active if k not in cues]:
            since = self.missing.setdefault(key, t)
            if t - since < CLEAR_AFTER_S:
                continue
            del self.missing[key]
            a = self.active.pop(key)
            a.state, a.t = "cleared", t
            changed.append(a)
        return changed


engines: dict[str, Engine] = {}
sid_wid: dict[str, str] = {}
state: dict = {"nc": None}


def engine(wid: str) -> Engine:
    if wid not in engines:
        engines[wid] = Engine(wid)
    return engines[wid]


async def send_alerts(wid: str, alerts: list[Alert], t0: float) -> None:
    data = AlertsData(alerts=alerts).model_dump(exclude_none=True)
    await publish(state["nc"], f"alerts.{wid}", Envelope(wid=wid, ts={"decision": [t0, time.time()]}, data=data))
    for a in alerts:
        log.info("%s %s [%s] %s", wid, a.state, a.rule, a.title)


async def send_clusters(wid: str, eng: Engine, t0: float) -> None:
    data = ClustersData(clusters=eng.ws.groups, t=eng.clock).model_dump(exclude_none=True)
    await publish(state["nc"], f"clusters.{wid}", Envelope(wid=wid, ts={"decision": [t0, time.time()]}, data=data))


async def on_objects(msg) -> None:
    env, _ = decode(msg)
    wid = msg.subject.split(".")[1]
    eng = engine(wid)
    if env.data["map"] != eng.map:  # a new map: nothing from the old one applies any more
        if eng.map is not None:
            cleared = eng.clear_now(list(eng.active))
            had_groups = bool(eng.ws.groups)
            eng.ws, eng.clock = WorldState(), None  # the world's clock restarts too
            eng.grouper = Grouper(CLUSTER_PARAMS)
            if had_groups:
                await send_clusters(wid, eng, time.time())
            if cleared:
                await send_alerts(wid, cleared, time.time())
        eng.map = env.data["map"]
    if env.data.get("t") is not None:
        eng.clock = max(eng.clock or 0.0, env.data["t"])
    ws = eng.ws
    for o in env.data.get("objects", []):
        ws.objects[o["oid"]] = o
    for oid in env.data.get("expired", []):
        o = ws.objects.pop(oid, None)
        if o:
            ws.gone[oid] = o
    forgotten = env.data.get("forgotten", [])
    for oid in forgotten:  # merged, or its replay went back: not a lost contact
        ws.objects.pop(oid, None)
    if forgotten:  # an alert about objects that are all forgotten now (a rewind) goes with them
        known = ws.objects.keys() | ws.gone.keys()
        cleared = eng.clear_now([k for k, a in eng.active.items() if a.objects and not known & set(a.objects)])
        if cleared:
            await send_alerts(wid, cleared, time.time())


async def on_events(msg) -> None:
    env, _ = decode(msg)
    engine(msg.subject.split(".")[1]).ws.events.extend(env.data.get("events", []))


async def on_geo(msg) -> None:
    env, _ = decode(msg)
    wid = env.wid
    sid_wid[env.sid] = wid
    eng = engine(wid)
    if eng.map is not None and env.data["map"] != eng.map:  # a drone the world leaves out
        return
    eng.ws.cameras[env.sid] = {"gsd_m": env.data.get("gsd_m"), "centre": env.data.get("centre"), "t": eng.now()}


async def loop() -> None:
    while True:
        await asyncio.sleep(PERIOD_S)
        wall = time.time()
        for wid, eng in list(engines.items()):
            changed = eng.step(eng.now())
            if changed:
                await send_alerts(wid, changed, wall)
            if eng.ws.groups or eng.had_clusters:  # an empty list tells the clients the last group is gone
                await send_clusters(wid, eng, wall)
            eng.had_clusters = bool(eng.ws.groups)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["nc"] = await connect("decision")
    await state["nc"].subscribe("world.*.objects", cb=on_objects)
    await state["nc"].subscribe("world.*.events", cb=on_events)
    await state["nc"].subscribe("geo.>", cb=on_geo)
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    await close(state["nc"])


app = FastAPI(title="blade decision-support", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "worlds": {w: len(e.active) for w, e in engines.items()}}


@app.get("/worlds/{wid}/rules")
async def get_rules(wid: str):
    return engine(wid).rules


@app.put("/worlds/{wid}/rules")
async def put_rules(wid: str, body: dict):
    eng = engine(wid)
    for name, params in body.items():
        if name not in RULES:
            raise HTTPException(400, f"unknown rule {name}; rules: {sorted(RULES)}")
        if not isinstance(params, dict):
            raise HTTPException(400, f"{name}: expected an object of parameters")
        eng.rules[name] = {**eng.rules.get(name, {}), **params}
    return eng.rules


@app.get("/worlds/{wid}/clusters")
async def clusters(wid: str):
    eng = engines.get(wid)
    return [] if eng is None else eng.ws.groups


@app.get("/worlds/{wid}/alerts")
async def alerts(wid: str, all: bool = False):
    eng = engines.get(wid)
    if eng is None:
        return []
    src = eng.history if all else eng.active.values()
    return [a.model_dump(exclude_none=True) for a in src]


def main() -> None:
    run_app("blade_decision.main:app", ports.DECISION)
