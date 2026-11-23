#!/usr/bin/env python3
"""How well the world model keeps one object per real object, scored against sim truth. `capture` saves
geo.<sid> and truth.<sid> of a replay; `score` runs them through fusion.World offline, with fusion.py
constants overridden by --set (e.g. --set GAP_S=3). "per true" 1.0 means every real object kept one ID."""
from __future__ import annotations

import argparse
import asyncio
import bisect
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

MATCH_M = {"vehicle": 15.0, "soldier": 8.0}  # an update this close to a true object is "of" it
WRONG_SHARE = 0.2  # a second true object with this share: a wrong merge
MIN_OBS = 5


def capture(args) -> int:
    import httpx

    from blade_common.bus import connect, decode

    async def run():
        nc = await connect("world-score")
        out = open(args.out, "w")

        async def on(msg):
            env, _ = decode(msg)
            out.write(json.dumps({"subj": msg.subject.split(".")[0], "sid": env.sid, "frame": env.frame,
                                  "ts": env.ts, "data": env.data}) + "\n")

        rec = httpx.get(f"{args.gateway}/api/recordings/{args.recording}", timeout=10)
        rec.raise_for_status()
        sids = {d["name"]: f"{args.wid}-{k}" for k, d in enumerate(rec.json()["drones"])}  # every drone of it
        for sid in sids.values():
            for s in (f"geo.{sid}", f"truth.{sid}"):
                await nc.subscribe(s, cb=on)
        r = httpx.post(f"{args.gateway}/api/replays", timeout=10,
                       json={"recording": args.recording, "wid": args.wid, "sids": sids})
        r.raise_for_status()
        dur = r.json()["duration_s"]
        print(f"replaying {args.recording} into {args.wid} ({', '.join(sids)}): {dur:.0f} s")
        await asyncio.sleep(dur + 3)
        httpx.delete(f"{args.gateway}/api/replays/{args.wid}", timeout=10)
        await nc.drain()
        out.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    asyncio.run(run())
    print(f"wrote {args.out}")
    return 0


def apply_sets(fusion, sets: list[str]) -> None:
    for s in sets:
        name, value = s.split("=", 1)
        name, _, key = name.partition(".")
        if key:
            getattr(fusion, name)[key] = float(value)
        else:
            setattr(fusion, name, float(value))


def score(args) -> int:
    from blade_world import fusion

    apply_sets(fusion, args.set or [])
    rows = [json.loads(line) for line in open(args.file)]
    truth = {r["data"]["frame"]: r["data"]["objs"] for r in rows if r["subj"] == "truth"}
    tframes = sorted(truth)
    geo = sorted((r for r in rows if r["subj"] == "geo"), key=lambda r: r["ts"]["capture"])
    if not geo or not truth:
        print("no geo or no truth in the file")
        return 1

    def truth_at(frame: int) -> list:
        i = bisect.bisect_left(tframes, frame)
        near = [tframes[j] for j in (i - 1, i) if 0 <= j < len(tframes)]
        return truth[min(near, key=lambda f: abs(f - frame))]

    w = fusion.World("score")
    labels: dict[str, Counter] = defaultdict(Counter)  # oid -> true id -> updates
    tracks_of: dict[str, set] = defaultdict(set)
    ever: dict = {}  # every object made, minus merged ones
    seen_truth: dict[str, set] = defaultdict(set)
    next_sweep = 0.0
    for r in geo:
        t = r["ts"]["capture"]
        changed, _ = w.observe(r["sid"], r["data"].get("pts", []), t)
        for oid in w.take_merged():  # move labels to the object that absorbed it
            keep = next((w.by_track[k] for k in tracks_of[oid] if k in w.by_track), None)
            if keep:
                labels[keep].update(labels.pop(oid, Counter()))
                tracks_of[keep] |= tracks_of.pop(oid, set())
            ever.pop(oid, None)
        objs = truth_at(r["frame"])
        pairs = []  # nearest pair first, one per object per frame
        for o in changed:
            ever[o.oid] = o
            tracks_of[o.oid] |= set(o.tracks)
            g = fusion.assoc(o.grp)
            pairs += [(math.dist(o.x[:2], tr[1][:2]), o.oid, tr[0], g) for tr in objs
                      if ("soldier" if tr[4] else "vehicle") == g]
        used_o, used_t = set(), set()
        for d, oid, tid, g in sorted(pairs):
            if d <= MATCH_M[g] and oid not in used_o and tid not in used_t:
                used_o.add(oid)
                used_t.add(tid)
                labels[oid][tid] += 1
                seen_truth[g].add(tid)
        if t >= next_sweep:
            w.sweep(t)
            next_sweep = t + 1.0

    print(f"{args.file}: {len(geo)} geo frames, {geo[-1]['ts']['capture'] - geo[0]['ts']['capture']:.0f} s")
    print(f"associations: {dict(w.stats)}")
    head = ("group", "true", "world", "per true", "split", "wrong merge", "no truth")
    print(f"{head[0]:8s} " + " ".join(f"{h:>{n}s}" for h, n in zip(head[1:], (5, 6, 8, 6, 12, 9))))
    for g in sorted(seen_truth):
        oids = [oid for oid, o in ever.items() if fusion.assoc(o.grp) == g and o.n >= MIN_OBS]
        owner = {oid: labels[oid].most_common(1)[0][0] for oid in oids if labels[oid]}
        split = sum(1 for tid in seen_truth[g] if sum(1 for v in owner.values() if v == tid) > 1)
        wrong = sum(1 for oid in oids if len(labels[oid]) > 1
                    and labels[oid].most_common(2)[1][1] >= WRONG_SHARE * sum(labels[oid].values()))
        no_truth = sum(1 for oid in oids if not labels[oid])
        n_true = len(seen_truth[g])
        per = len(oids) / max(n_true, 1)
        print(f"{g:8s} {n_true:5d} {len(oids):6d} {per:8.2f} {split:6d} {wrong:12d} {no_truth:9d}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("recording")
    c.add_argument("out")
    c.add_argument("--wid", default=f"score-{int(time.time()) % 100000}",
                   help="the replay's world id; its drones play as <wid>-0, <wid>-1, ...")
    c.add_argument("--gateway", default="http://127.0.0.1:8000")
    s = sub.add_parser("score")
    s.add_argument("file")
    s.add_argument("--set", action="append", help="NAME=value or NAME.key=value, a constant in fusion.py")
    args = ap.parse_args()
    return capture(args) if args.cmd == "capture" else score(args)


if __name__ == "__main__":
    sys.exit(main())
