"""The clock of one world. A live stream runs on the wall clock. A replay carries its recording time (ts.media), and
the world runs it on that time instead: a paused replay stands still, a replay at x2 runs twice as fast, so objects
do not go stale while paused and speeds come out the same at any replay speed. Each replayed stream joins at the
world's current time (an offset to its recording time), and joins again when it went back in the recording (a seek
back, a loop: a rewind, and what every stream of the world saw is forgotten) or fell behind the world (paused on
its own while the world went on). A world that has a live stream follows the wall clock for all its streams, so
objects of a drone that stops sending still go stale. The clock never goes back."""
from __future__ import annotations

JUMP_BACK_S = 0.5  # s of recording time a stream may go back before it counts as a rewind (seek back, loop)
LAG_S = 5.0  # s a stream may fall behind the world's time before it joins again


class WorldClock:
    def __init__(self, wall: float, live_keep_s: float = 600.0):
        self.t = wall
        self.live_keep_s = live_keep_s  # s a live stream keeps the world on the wall clock after its last frame
        self.live_at: float | None = None  # wall time of the last live frame
        self.lead = 0.0  # s the world was ahead of the wall when it went live (a replay played above x1)
        self.offset: dict[str, float] = {}  # sid -> world time minus recording time
        self.media: dict[str, float] = {}  # sid -> last recording time

    def _live(self, wall: float) -> bool:
        return self.live_at is not None and wall - self.live_at <= self.live_keep_s

    def frame(self, sid: str, media: float | None, wall: float) -> tuple[float, bool]:
        """-> (the frame's time in this world, whether the stream went back in its recording)"""
        if media is None:
            if not self._live(wall):
                self.lead = max(0.0, self.t - wall)
            self.live_at = wall
            return self.tick(wall), False
        last = self.media.get(sid)
        rewind = last is not None and media < last - JUMP_BACK_S
        if self._live(wall) or last is None or rewind or media + self.offset[sid] < self.t - LAG_S:
            self.offset[sid] = self.tick(wall) - media
        self.media[sid] = media
        t = media + self.offset[sid]
        self.t = max(self.t, t)
        return t, rewind

    def rejoin_all(self, sid: str) -> None:
        """The world went back with sid: every other stream joins again at the world's time with its next frame,
        instead of counting its next frame (earlier in its recording than its last) as a rewind of its own."""
        self.media = {sid: self.media[sid]}
        self.offset = {sid: self.offset[sid]}

    def tick(self, wall: float) -> float:
        if self._live(wall):
            self.t = max(self.t, wall + self.lead)
        return self.t
