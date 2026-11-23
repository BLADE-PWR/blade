"""The timeline every drone of a replay plays on: its position (s from the start of the mission recording), speed,
pause and loop. Each drone's track waits on it before sending a chunk (wait_until), so the drones stay together
through pause, speed changes, seeks and loops. A seek or a loop is a jump (generation goes up): tracks then find their
place again. A step lets every track send its next N frames while paused."""
from __future__ import annotations

import asyncio
import time


class ReplayClock:
    def __init__(self, duration_s: float, speed: float = 1.0, loop: bool = False, start_s: float = 0.0):
        self.duration_s = duration_s
        self.speed = speed
        self.loop = loop
        self.paused = False
        self.at_end = False  # paused at the end: Play starts over, a seek goes on from there
        self.generation = 0  # goes up with every jump (seek, loop)
        self.steps = 0  # frames asked for with step, in total: each track sends the ones it has not sent yet
        self._pos0 = min(max(start_s, 0.0), duration_s)  # position at _t0 ...
        self._t0 = time.monotonic()  # ... from where it runs at speed while not paused
        self._changed = asyncio.Event()

    def position(self) -> float:
        if self.paused:
            return self._pos0
        return min(self._pos0 + (time.monotonic() - self._t0) * self.speed, self.duration_s)

    def _replan(self) -> None:
        self._pos0, self._t0 = self.position(), time.monotonic()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def set_speed(self, speed: float) -> None:
        self._replan()
        self.speed = speed
        self._notify()

    def set_paused(self, paused: bool) -> None:
        if not paused and self.at_end:  # Play at the end starts over
            self.seek(0.0)
            return
        self._replan()
        self.paused = paused
        self._notify()

    def set_loop(self, loop: bool) -> None:
        self.loop = loop
        if loop and self.at_end:  # waiting at the end: go round
            self.seek(0.0)

    def seek(self, t: float) -> None:
        """Jump to t; at the end it also plays on from there, like a player."""
        if self.at_end:
            self.paused = self.at_end = False
        self._pos0, self._t0 = min(max(t, 0.0), self.duration_s), time.monotonic()
        self.generation += 1
        self._notify()

    def step(self, n: int) -> None:
        if self.at_end or n <= 0:
            return
        self.set_paused(True)
        self.steps += n
        self._notify()

    def stepped_to(self, t: float) -> None:
        """A track sent frames up to t on a step: the paused position follows the furthest track."""
        if self.paused:
            self._pos0 = max(self._pos0, min(t, self.duration_s))

    def end(self) -> None:
        """The end was reached without loop: paused there."""
        self._pos0, self.paused, self.at_end = self.duration_s, True, True
        self._notify()

    async def wait_until(self, t: float, generation: int, steps: int | None = None) -> str:
        """Wait until the position reaches t. -> "reached", "jumped" (a seek or loop since generation), or "step"
        (more frames asked for than steps: only for a caller that passes steps)."""
        while True:
            if self.generation != generation:
                return "jumped"
            if steps is not None and self.steps != steps:
                return "step"
            pos = self.position()
            if pos >= t:
                return "reached"
            changed = self._changed
            delay = None if self.paused else (t - pos) / self.speed
            try:
                await asyncio.wait_for(changed.wait(), delay)
            except TimeoutError:
                pass

    async def jumped(self, generation: int) -> None:
        """Wait for the next seek or loop."""
        while self.generation == generation:
            await self._changed.wait()
