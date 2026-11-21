"""evaluator: truth + registry + geo.<sid> + world.<wid>.objects -> eval.<wid> (2/s): CEP per group,
detection counts, false objects. Matched samples also go to EVAL_DIR/*.csv (tools/eval_report.py)."""
from __future__ import annotations

import asyncio
import csv
import logging
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from blade_common import ports
from blade_common.bus import close, connect, decode, publish
from blade_common.contracts import Envelope, EvalData
from blade_common.service import env_float, env_str, run_app
from blade_common.util import inside

from .evaluate import TRUTH_GROUP, StreamTruth, WorldEval, match

log = logging.getLogger("evaluator")
EVAL_DIR = Path(env_str("EVAL_DIR", "state/eval"))
WINDOW_S = env_float("WINDOW_S", 30.0)
COLUMNS = ["t", "level", "wid", "sid", "frame", "grp", "truth_id", "est", "err_m", "tx", "ty", "ex", "ey"]


class Evaluator:
    def __init__(self):
        self.truth: dict[str, StreamTruth] = defaultdict(StreamTruth)
        self.sid_wid: dict[str, str] = {}
        self.sid_map: dict[str, str] = {}  # from geo
        self.world_map: dict[str, str] = {}  # from world.<wid>.objects; drones on another map are left out
        self.objects: dict[str, dict[str, dict]] = defaultdict(dict)
        self.evals: dict[str, WorldEval] = {}
        self.writers: dict[str, tuple] = {}
        self.nc = None

    def ev(self, wid: str) -> WorldEval:
        if wid not in self.evals:
            self.evals[wid] = WorldEval(wid, WINDOW_S)
        return self.evals[wid]

    def write(self, wid: str, row: list) -> None:
        if wid not in self.writers:
            EVAL_DIR.mkdir(parents=True, exist_ok=True)
            f = open(EVAL_DIR / f"eval_{wid}_{time.strftime('%Y%m%d_%H%M%S')}.csv", "w", newline="")
            w = csv.writer(f)
            w.writerow(COLUMNS)
            self.writers[wid] = (f, w)
        self.writers[wid][1].writerow(row)

    async def on_truth(self, msg) -> None:
        env, _ = decode(msg)
        if env.wid:
            self.sid_wid[env.sid] = env.wid
        if msg.subject.startswith("registry."):
            self.truth[env.sid].spawn(env.data)
        else:
            self.truth[env.sid].gt(env.data)

    async def on_geo(self, msg) -> None:
        env, _ = decode(msg)
        sid, wid = env.sid, env.wid or "default"
        self.sid_wid[sid] = wid
        self.sid_map[sid] = env.data["map"]
        if self.world_map.get(wid, env.data["map"]) != env.data["map"]:
            return
        st = self.truth[sid]
        fp = [p for p in env.data.get("footprint", []) if p]
        st.footprint = fp if len(fp) >= 3 else None
        truth = st.in_view(env.frame)
        if not truth:
            return
        est = [(k, p["grp"], (p["xyz"][0], p["xyz"][1])) for k, p in enumerate(env.data.get("pts", []))
               if p.get("xyz") and p["grp"] in TRUTH_GROUP.values()]
        pairs, _, _ = match(truth, est)
        t = time.time()
        txy = {tid: xy for tid, _, xy in truth}
        ev = self.ev(wid)
        for tid, k, err in pairs:
            grp, exy = est[k][1], est[k][2]
            ev.add("geo", grp, err, t)
            self.write(wid, [round(t, 3), "geo", wid, sid, env.frame, grp, tid, k, round(err, 3),
                             *txy[tid], *exy])

    async def on_world(self, msg) -> None:
        env, _ = decode(msg)
        wid = msg.subject.split(".")[1]
        self.world_map[wid] = env.data["map"]
        objs = self.objects[wid]
        for o in env.data.get("objects", []):
            objs[o["oid"]] = o
        for oid in env.data.get("expired", []) + env.data.get("forgotten", []):
            objs.pop(oid, None)

    async def tick(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            t = time.time()
            for wid in sorted(set(self.sid_wid.values())):
                try:
                    await self.evaluate(wid, t)
                except Exception:
                    log.exception("eval %s", wid)
            for f, _ in self.writers.values():
                f.flush()

    async def evaluate(self, wid: str, t: float) -> None:
        sids = [s for s, w in self.sid_wid.items() if w == wid and self.sid_map.get(s) == self.world_map.get(wid)]
        # streams of one world film one game: an object in view of two drones counts once
        seen: dict[int, tuple] = {}
        footprints = []
        for sid in sids:
            st = self.truth[sid]
            for tid, grp, xy in st.in_view():
                seen[tid] = ((sid, tid), grp, xy)
            if st.footprint:
                footprints.append(st.footprint)
        truth = list(seen.values())
        active = [o for o in self.objects[wid].values() if o.get("state") == "active"]
        est = [(o["oid"], o["grp"], tuple(o["xy"])) for o in active]
        pairs, _, unmatched_e = match(truth, est)
        ev = self.ev(wid)
        by_truth = {tkey: (oid, err) for tkey, oid, err in pairs}
        out = []
        for tkey, grp, xy in truth:
            oid, err = by_truth.get(tkey, (None, None))
            out.append({"truth_id": tkey[1], "grp": grp, "xy": xy, "oid": oid,
                        "err_m": None if err is None else round(err, 2)})
            ev.detect[grp][1] += 1
            if oid is not None:
                ev.detect[grp][0] += 1
                ev.add("world", grp, err, t)
                exy = next(e[2] for e in est if e[0] == oid)
                self.write(wid, [round(t, 3), "world", wid, tkey[0], None, grp, tkey[1], oid, round(err, 3), *xy, *exy])
        false = [est[i][0] for i in unmatched_e if any(inside(fp, *est[i][2]) for fp in footprints)]
        ev.trim(t)
        summ = ev.summary("world")
        groups = {}
        for grp in {x["grp"] for x in out} | set(summ):
            mine = [x for x in out if x["grp"] == grp]
            groups[grp] = {**summ.get(grp, {"n": 0}), "detected": sum(x["oid"] is not None for x in mine),
                           "in_view": len(mine)}
        data = EvalData.model_validate({"window_s": WINDOW_S, "groups": groups, "truth": out, "false_objects": false})
        await publish(self.nc, f"eval.{wid}", Envelope(wid=wid, ts={"evaluator": [t, time.time()]},
                                                         data=data.model_dump(exclude_none=True)))


svc = Evaluator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc.nc = await connect("evaluator")
    for subj in ("truth.>", "registry.>"):
        await svc.nc.subscribe(subj, cb=svc.on_truth)
    await svc.nc.subscribe("geo.>", cb=svc.on_geo)
    await svc.nc.subscribe("world.*.objects", cb=svc.on_world)
    task = asyncio.create_task(svc.tick())
    yield
    task.cancel()
    for f, _ in svc.writers.values():
        f.close()
    await close(svc.nc)


app = FastAPI(title="blade evaluator", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"ok": True, "worlds": sorted(svc.evals), "csv": str(EVAL_DIR)}


def _ev(wid: str) -> WorldEval:
    if wid not in svc.evals:
        raise HTTPException(404, f"no evaluation for {wid} (no truth or geo seen)")
    return svc.evals[wid]


@app.get("/worlds/{wid}/eval")
async def window(wid: str):
    ev = _ev(wid)
    return {"wid": wid, "window_s": ev.window_s, "world": ev.summary("world"), "geo": ev.summary("geo")}


@app.get("/worlds/{wid}/eval/report")
async def report(wid: str):
    return {"wid": wid, **_ev(wid).report()}


@app.post("/worlds/{wid}/eval/reset")
async def reset(wid: str):
    svc.evals.pop(wid, None)
    return {"wid": wid, "reset": True}


def main() -> None:
    run_app("blade_evaluator.main:app", ports.EVALUATOR)
