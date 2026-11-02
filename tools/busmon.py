#!/usr/bin/env python3
"""Watch the bus: message rate, size and the newest envelope per subject.
    uv run python tools/busmon.py "frames.>" "geo.>" --seconds 10 --json"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import defaultdict

from blade_common.bus import ENV_HEADER, connect


async def run(subjects: list[str], seconds: float, as_json: bool) -> None:
    nc = await connect("busmon")
    stats = defaultdict(lambda: {"n": 0, "bytes": 0, "last": None})

    async def cb(msg):
        s = stats[msg.subject]
        s["n"] += 1
        s["bytes"] += len(msg.data)
        if msg.headers and ENV_HEADER in msg.headers:
            s["last"] = json.loads(msg.headers[ENV_HEADER])
        elif not msg.subject.startswith("raw."):
            try:
                s["last"] = json.loads(msg.data)
            except ValueError:
                pass

    for subj in subjects:
        await nc.subscribe(subj, cb=cb)
    t0 = time.time()
    try:
        while not seconds or time.time() - t0 < seconds:
            await asyncio.sleep(2 if not seconds else min(2, seconds))
            if not as_json:
                dt = time.time() - t0
                print(f"--- {dt:.0f} s")
                for subj in sorted(stats):
                    s = stats[subj]
                    print(f"{subj:<36}{s['n'] / dt:8.1f}/s {s['bytes'] / max(s['n'], 1) / 1024:8.1f} KB")
    finally:
        dt = time.time() - t0
        if as_json:
            print(json.dumps({k: {"rate": round(v["n"] / dt, 2), "n": v["n"], "last": v["last"]}
                              for k, v in sorted(stats.items())}, indent=1, default=str))
        await nc.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("subjects", nargs="*", default=[">"])
    ap.add_argument("--seconds", type=float, default=0)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    asyncio.run(run(a.subjects, a.seconds, a.json))


if __name__ == "__main__":
    main()
