"""A replay pushes a recorded stream.ts into ingest (POST /push/{sid}) and its truth into the truth port
under a new stream id; from ingest onwards it is a live stream. Pacing: realtime | max. Delivery:
latest | all (every frame through every stage). Control: pause, speed, seek, step N frames."""
from __future__ import annotations

import asyncio
import bisect
import contextlib
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .recording import frame_ends, load_index, load_truth

log = logging.getLogger("sessions.replay")


@dataclass
class Replay:
    sid: str
    recording: str
    dir: Path
    ingest_url: str
    truth_addr: tuple[str, int]
    wid: str | None = None
    speed: float = 1.0
    loop: bool = False
    offset_s: float = 0.0
    pacing: str = "realtime"
    delivery: str = "latest"
    send_truth: bool = True
    state: str = "starting"
    position_s: float = 0.0
    loops: int = 0
    error: str | None = None
    paused: bool = False
    _seek: float | None = None
    _steps: int = 0
    _task: asyncio.Task | None = None
    _wake: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self):
        self.index = load_index(self.dir)
        if not self.index:
            raise ValueError(f"{self.recording}: empty index")
        t0 = self.index[0]["t"]
        self.times = [e["t"] - t0 for e in self.index]
        self.offsets = [e["o"] for e in self.index]
        self.size = self.index[-1]["o"] + self.index[-1]["n"]
        self.truth = []
        if self.send_truth:
            self.truth = sorted((dict(r, t=r["t"] - t0) for r in load_truth(self.dir)), key=lambda r: r["t"])
        self.truth_times = [r["t"] for r in self.truth]
        self.duration_s = self.times[-1]
        self.frames: list[int] | None = None  # frame_ends, loaded on the first step
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"replay-{self.sid}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self.state = "stopped"

    def patch(self, paused: bool | None = None, speed: float | None = None, seek_s: float | None = None,
              step: int | None = None) -> None:
        if speed is not None and speed > 0:
            self.speed = speed
        if seek_s is not None:
            self._seek = max(0.0, min(seek_s, self.duration_s))
        if step:
            paused = True
            self._steps += max(int(step), 0)
        if paused is not None:
            self.paused = paused
            if self.state in ("playing", "paused"):
                self.state = "paused" if paused else "playing"
        self._wake.set()

    def info(self) -> dict:
        return {"sid": self.sid, "recording": self.recording, "wid": self.wid, "state": self.state,
                "position_s": round(self.position_s, 3), "duration_s": round(self.duration_s, 2),
                "speed": self.speed, "paused": self.paused, "loop": self.loop, "loops": self.loops,
                "pacing": self.pacing, "delivery": self.delivery, "error": self.error}

    def _send_truth(self, r: dict) -> None:
        msg = {"sid": self.sid, "kind": r["kind"], "t": time.time(), "rec": r["rec"], "wid": self.wid}
        try:
            self.sock.sendto(json.dumps(msg, separators=(",", ":")).encode(), self.truth_addr)
        except OSError as e:
            log.debug("truth send: %s", e)

    def _chunk_at(self, byte: int) -> int:
        return max(bisect.bisect_right(self.offsets, byte) - 1, 0)

    def _time_at(self, byte: int) -> float:
        return self.times[self._chunk_at(byte)]

    def _byte_at(self, t: float) -> int:
        return self.offsets[min(bisect.bisect_left(self.times, t), len(self.offsets) - 1)]

    async def _frame_ends(self) -> list[int]:
        if self.frames is None:
            self.frames = await asyncio.to_thread(frame_ends, self.dir / "stream.ts")
        return self.frames

    async def _sleep_or_wake(self, delay: float) -> bool:
        """-> True if a control change woke us up."""
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
            return True
        except TimeoutError:
            return False

    async def _body(self):
        f = open(self.dir / "stream.ts", "rb")
        j = 0

        def read(a: int, b: int) -> bytes:
            f.seek(a)
            return f.read(b - a)

        def truth_until(t_media: float):
            nonlocal j
            while j < len(self.truth) and self.truth[j]["t"] <= t_media:
                if self.truth[j]["kind"] != "spawn":
                    self._send_truth(self.truth[j])
                j += 1

        try:
            byte = self._byte_at(self.offset_s)
            while True:
                for r in self.truth:  # every spawn first, before any gt mentions it
                    if r["kind"] == "spawn":
                        self._send_truth(r)
                j = bisect.bisect_left(self.truth_times, self._time_at(byte))
                clock0, media0 = time.monotonic(), self._time_at(byte)
                self.state = "paused" if self.paused else "playing"
                while byte < self.size:
                    if self._seek is not None:
                        byte, self._seek = self._byte_at(self._seek), None
                        j = bisect.bisect_left(self.truth_times, self._time_at(byte))
                        clock0, media0 = time.monotonic(), self._time_at(byte)
                        self.position_s = media0
                    if self.paused:
                        self.state = "paused"
                        if self._steps:
                            ends = await self._frame_ends()
                            k = bisect.bisect_right(ends, byte)  # frames already complete
                            target = ends[min(k + self._steps - 1, len(ends) - 1)]
                            self._steps = 0
                            truth_until(self._time_at(target))
                            yield read(byte, target)
                            byte = target
                            self.position_s = self._time_at(byte)
                            continue
                        await self._sleep_or_wake(1.0)
                        clock0, media0 = time.monotonic(), self._time_at(byte)
                        continue
                    self.state = "playing"
                    i = self._chunk_at(byte)
                    t_media = self.times[i]
                    if self.pacing == "realtime":
                        delay = clock0 + (t_media - media0) / self.speed - time.monotonic()
                        if delay > 0 and await self._sleep_or_wake(delay):
                            clock0, media0 = time.monotonic(), self._time_at(byte)
                            continue  # seek / pause / speed changed: re-plan
                    truth_until(t_media)
                    end = self.offsets[i] + self.index[i]["n"]
                    yield read(byte, end)
                    byte = end
                    self.position_s = t_media
                if not self.loop:
                    break
                self.loops += 1
                byte = 0
        finally:
            f.close()

    async def _run(self) -> None:
        url = f"{self.ingest_url}/push/{self.sid}"
        params = {k: v for k, v in (("wid", self.wid), ("delivery", self.delivery)) if v}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5)) as client:
                r = await client.post(url, params=params, content=self._body())
                r.raise_for_status()
                log.info("replay %s done: %s", self.sid, r.json())
            self.state = "finished"
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as e:
            self.state, self.error = "failed", str(e)
            log.exception("replay %s failed", self.sid)
