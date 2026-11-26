"""Detector replicas and which streams each one runs. A tracker belongs to one stream, so streams are
assigned to replicas (KV "assign", set by the gateway), not load-balanced per frame. An unassigned
stream goes to the replica rendezvous hashing picks; without JetStream a replica runs everything."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import socket
import time

from blade_common.bus import kv_bucket, kv_read

from .routing import Routing

log = logging.getLogger("detector.cluster")
HEARTBEAT_S = 2.0
ROUTING_KEY = "auto"


def rendezvous(sid: str, instances: list[str]) -> str | None:
    return max(instances, key=lambda i: hashlib.sha1(f"{sid}|{i}".encode()).digest(), default=None)


class Cluster:
    def __init__(self, instance: str | None = None):
        self.me = instance or os.environ.get("INSTANCE") or f"{socket.gethostname()}-{os.getpid()}"
        self.live: list[str] = [self.me]
        self.peers: dict[str, dict] = {}  # instance -> its last heartbeat (models, streams, auto)
        self.assign: dict[str, dict] = {}
        self.kv_det = None
        self.kv_assign = None
        self.kv_routing = None
        self.routing: Routing | None = None  # the auto routing as the operator set it, None: AUTO_MODELS

    async def connect(self, nc) -> None:
        try:
            js = nc.jetstream()
            self.kv_det = await kv_bucket(js, "detectors", ttl=10)
            self.kv_assign = await kv_bucket(js, "assign")
            self.kv_routing = await kv_bucket(js, "routing")
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

    def lacking(self, model: str) -> list[str]:
        """The other live replicas whose last heartbeat does not list the model as loaded."""
        return [i for i in self.live if i != self.me and model not in self.peers.get(i, {}).get("models", [])]

    async def set_routing(self, routing: Routing | None) -> None:
        self.routing = routing
        if self.kv_routing is None:  # no JetStream: the routing of this replica only
            return
        if routing is None:
            with contextlib.suppress(Exception):  # nothing to delete when it was never set
                await self.kv_routing.delete(ROUTING_KEY)
        else:
            await self.kv_routing.put(ROUTING_KEY, routing.model_dump_json(exclude_none=True).encode())

    def _take_routing(self, raw: dict | None) -> None:
        """The routing KV holds, parsed once per refresh; a change is logged, so a routing set in the UI that
        outlives a restart shows in the log."""
        try:
            new = Routing.model_validate(raw) if raw else None
        except ValueError:
            log.exception("bad auto routing in KV: keeping the previous one")
            return
        if new != self.routing:
            log.info("auto routing: %s", f"set in the UI, replaces AUTO_MODELS until DELETE /routing: "
                     f"{new.model_dump(exclude_none=True)}" if new else "AUTO_MODELS")
            self.routing = new

    async def run(self, status) -> None:
        """Heartbeat and refresh; status() -> dict with this replica's models and streams."""
        while True:
            if self.kv_det is not None:
                try:
                    await self.kv_det.put(self.me, json.dumps({"instance": self.me, "t": time.time(),
                                                               **status()}).encode())
                    t = time.time()
                    dets = await kv_read(self.kv_det)
                    self.peers = {k: v for k, v in dets.items() if t - v.get("t", 0) < 3 * HEARTBEAT_S}
                    self.live = sorted(self.peers) or [self.me]
                    self.assign = await kv_read(self.kv_assign)
                    if self.kv_routing is not None:  # else what PUT /routing set stays
                        self._take_routing((await kv_read(self.kv_routing)).get(ROUTING_KEY))
                except Exception:
                    log.exception("cluster refresh")
            await asyncio.sleep(HEARTBEAT_S)
