"""Detector replicas and which streams each one runs. A tracker belongs to one stream, so streams are
assigned to replicas (KV "assign", set by the gateway), not load-balanced per frame. An unassigned
stream goes to the replica rendezvous hashing picks; without JetStream a replica runs everything."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time

from blade_common.bus import kv_bucket, kv_read

log = logging.getLogger("detector.cluster")
HEARTBEAT_S = 2.0


def rendezvous(sid: str, instances: list[str]) -> str | None:
    return max(instances, key=lambda i: hashlib.sha1(f"{sid}|{i}".encode()).digest(), default=None)


class Cluster:
    def __init__(self, instance: str | None = None):
        self.me = instance or os.environ.get("INSTANCE") or f"{socket.gethostname()}-{os.getpid()}"
        self.live: list[str] = [self.me]
        self.assign: dict[str, dict] = {}
        self.kv_det = None
        self.kv_assign = None

    async def connect(self, nc) -> None:
        try:
            js = nc.jetstream()
            self.kv_det = await kv_bucket(js, "detectors", ttl=10)
            self.kv_assign = await kv_bucket(js, "assign")
        except Exception as e:
            log.warning("JetStream KV unavailable (%s): this replica runs every stream", e)

    def owner(self, sid: str) -> str | None:
        a = self.assign.get(sid)
        if a and a.get("instance") in self.live:
            return a["instance"]
        return rendezvous(sid, self.live)

    def mine(self, sid: str) -> bool:
        return self.kv_det is None or self.owner(sid) == self.me

    def model(self, sid: str) -> str | None:
        return (self.assign.get(sid) or {}).get("model")

    def conf(self, sid: str) -> float | None:
        return (self.assign.get(sid) or {}).get("conf")

    async def move(self, old: str, new: str, fields: tuple[str, ...]) -> None:
        """The same drone under a new stream id: its settings (fields) go along, the old entry goes."""
        a = self.assign.pop(old, None)
        keep = {k: a[k] for k in fields if a and a.get(k) is not None}
        if keep and not any(self.assign.get(new, {}).get(k) is not None for k in fields):  # not set for it already
            await self.set(new, **keep)
        if a is not None and self.kv_assign is not None:
            await self.kv_assign.delete(old)

    async def set(self, sid: str, **fields) -> None:
        """Merges fields into the stream's entry as KV has it now, not as the last refresh saw it."""
        cur = (await kv_read(self.kv_assign, sid)).get(sid) if self.kv_assign is not None else None
        self.assign[sid] = a = dict(cur or self.assign.get(sid) or {}, **fields)
        if self.kv_assign is not None:
            await self.kv_assign.put(sid, json.dumps(a).encode())

    async def run(self, status) -> None:
        """Heartbeat and refresh; status() -> dict with this replica's models and streams."""
        while True:
            if self.kv_det is not None:
                try:
                    await self.kv_det.put(self.me, json.dumps({"instance": self.me, "t": time.time(),
                                                               **status()}).encode())
                    t = time.time()
                    dets = await kv_read(self.kv_det)
                    self.live = sorted(k for k, v in dets.items() if t - v.get("t", 0) < 3 * HEARTBEAT_S) or [self.me]
                    self.assign = await kv_read(self.kv_assign)
                except Exception:
                    log.exception("cluster refresh")
            await asyncio.sleep(HEARTBEAT_S)
