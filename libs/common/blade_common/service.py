"""Config from the environment, per-stream diag, running uvicorn."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .bus import jdump
from .contracts import DiagData, StreamDiag

log = logging.getLogger("blade")


def env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_bool(name: str, default: bool) -> bool:
    """0 or 1; anything else is an error."""
    v = os.environ.get(name)
    if v is None:
        return default
    if v not in ("0", "1"):
        raise ValueError(f"{name} must be 0 or 1, got {v!r}")
    return v == "1"


def setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@dataclass
class _Counter:
    n_in: int = 0
    n_out: int = 0
    dropped: int = 0
    skipped: int = 0
    queue: int = 0
    latency: list[float] = field(default_factory=list)
    clock: float | None = None


class Diag:
    """Counts frames per stream and publishes diag.<service> once a second."""

    def __init__(self, service: str):
        self.service = service
        self.instance = os.environ.get("INSTANCE", socket.gethostname())
        self._c: dict[str, _Counter] = defaultdict(_Counter)
        self._t = time.time()

    def got(self, sid: str, n: int = 1) -> None:
        self._c[sid].n_in += n

    def sent(self, sid: str, n: int = 1, latency_s: float | None = None) -> None:
        c = self._c[sid]
        c.n_out += n
        if latency_s is not None:
            c.latency.append(latency_s)

    def clock(self, sid: str, arrival_minus_capture_s: float) -> None:
        c = self._c[sid]
        c.clock = arrival_minus_capture_s if c.clock is None else min(c.clock, arrival_minus_capture_s)

    def drop(self, sid: str, n: int = 1) -> None:
        self._c[sid].dropped += n

    def skip(self, sid: str, n: int = 1) -> None:
        self._c[sid].skipped += n

    def queue(self, sid: str, depth: int) -> None:
        self._c[sid].queue = depth

    def snapshot(self) -> DiagData:
        t = time.time()
        dt = max(t - self._t, 1e-6)
        self._t = t
        streams = {}
        for sid, c in self._c.items():
            lat = sorted(c.latency)
            streams[sid] = StreamDiag(fps_in=round(c.n_in / dt, 1), fps_out=round(c.n_out / dt, 1),
                                      queue=c.queue, dropped=c.dropped, skipped=c.skipped,
                                      latency_ms=round(lat[len(lat) // 2] * 1000, 1) if lat else None,
                                      clock_ms=None if c.clock is None else round(c.clock * 1000, 1))
        self._c = defaultdict(_Counter, {sid: _Counter(queue=c.queue) for sid, c in self._c.items()
                                         if c.n_in or c.n_out or c.queue})
        return DiagData(service=self.service, instance=self.instance, streams=streams)

    async def run(self, nc, period: float = 1.0) -> None:
        while True:
            await asyncio.sleep(period)
            try:
                snap = self.snapshot()
                await nc.publish(f"diag.{self.service}",
                                 jdump({"v": 1, "ts": {}, "data": snap.model_dump(exclude_none=True)}))
            except Exception:
                log.exception("diag publish failed")


def run_app(app_path: str, default_port: int) -> None:
    import uvicorn

    setup_logging()
    uvicorn.run(app_path, host=env_str("HOST", "0.0.0.0"), port=env_int("PORT", default_port),
                log_level=os.environ.get("LOG_LEVEL", "info").lower())
