"""decision-support: world.<wid>.objects / .events + geo.<sid> -> alerts.<wid> (rules.py), published when
an alert appears and when it clears. Rules run on the world's clock (WorldObjectsData.t), so a paused replay raises
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
from blade_common.contracts import Alert, AlertsData, Envelope
from blade_common.service import env_float, run_app

from .rules import DEFAULTS, RULES, WorldState

log = logging.getLogger("decision")
PERIOD_S = env_float("PERIOD_S", 1.0)
CLEAR_AFTER_S = env_float("CLEAR_AFTER_S", 5.0)
GONE_KEEP_S = env_float("GONE_KEEP_S", 600.0)


class Engine:
    def __init__(self, wid: str):
        self.wid = wid
        self.ws = WorldState()
        self.rules = copy.deepcopy(DEFAULTS)
        self.active: dict[str, Alert] = {}
        self.history: deque[Alert] = deque(maxlen=200)
        self.missing: dict[str, float] = {}  # alert key -> when its condition became false
        self.clock: float | None = None  # the world's time, s, from its object updates
        self._ids = itertools.count(1)

    def now(self) -> float:
        return time.time() if self.clock is None else self.clock

    def step(self, t: float) -> list[Alert]:
        """Run every enabled rule; -> alerts raised or cleared now."""
        self.ws.events = [e for e in self.ws.events if t - e["t"] < 30]
        self.ws.gone = {k: v for k, v in self.ws.gone.items() if t - v["last"] < GONE_KEEP_S}
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
            if key in self.active or not c.objects:
                continue
            for old_key, a in list(self.active.items()):
                if a.rule == c.rule and old_key not in cues and \
                        len(set(a.objects) & set(c.objects)) * 2 >= max(len(a.objects), len(c.objects)):
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


async def on_objects(msg) -> None:
    env, _ = decode(msg)
    eng = engine(msg.subject.split(".")[1])
    if env.data.get("t") is not None:
        eng.clock = max(eng.clock or 0.0, env.data["t"])
    ws = eng.ws
    for o in env.data.get("objects", []):
        ws.objects[o["oid"]] = o
    for oid in env.data.get("expired", []):
        o = ws.objects.pop(oid, None)
        if o:
            ws.gone[oid] = o
    for oid in env.data.get("forgotten", []):  # merged, or its replay went back: not a lost contact
        ws.objects.pop(oid, None)


async def on_events(msg) -> None:
    env, _ = decode(msg)
    engine(msg.subject.split(".")[1]).ws.events.extend(env.data.get("events", []))


async def on_geo(msg) -> None:
    env, _ = decode(msg)
    wid = env.wid or "default"
    sid_wid[env.sid] = wid
    eng = engine(wid)
    eng.ws.cameras[env.sid] = {"gsd_m": env.data.get("gsd_m"), "centre": env.data.get("centre"), "t": eng.now()}


async def loop() -> None:
    while True:
        await asyncio.sleep(PERIOD_S)
        wall = time.time()
        for wid, eng in list(engines.items()):
            changed = eng.step(eng.now())
            if changed:
                data = AlertsData(alerts=changed).model_dump(exclude_none=True)
                await publish(state["nc"], f"alerts.{wid}", Envelope(wid=wid, ts={"decision": [wall, time.time()]},
                                                                      data=data))
                for a in changed:
                    log.info("%s %s [%s] %s", wid, a.state, a.rule, a.title)


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


@app.get("/worlds/{wid}/alerts")
async def alerts(wid: str, all: bool = False):
    eng = engines.get(wid)
    if eng is None:
        return []
    src = eng.history if all else eng.active.values()
    return [a.model_dump(exclude_none=True) for a in src]


def main() -> None:
    run_app("blade_decision.main:app", ports.DECISION)
