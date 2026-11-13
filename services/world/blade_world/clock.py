"""The clock of one world. A live stream runs on the wall clock. A replay carries its recording time (ts.media), and
the world runs it on that time instead: a paused replay stands still, a replay at x2 runs twice as fast, so objects
do not go stale while paused and speeds come out the same at any replay speed. Each replayed stream joins at the
world's current time (an offset to its recording time); going back in the recording (a seek back, a loop) is a
rewind: the stream joins again and what it saw is forgotten. A world that has a live stream follows the wall clock
for all its streams, so objects of a drone that stops sending still go stale. The clock never goes back."""
from __future__ import annotations

JUMP_BACK_S = 0.5  # s of recording time a stream may go back before it counts as a rewind (seek back, loop)
JUMP_AHEAD_S = 600.0  # s: further ahead is another recording, not a seek, so the stream joins again


class WorldClock:
    def __init__(self, wall: float, live_keep_s: float = 600.0):
        self.t = wall
        self.live_keep_s = live_keep_s  # s a live stream keeps the world on the wall clock after its last frame
        self.live_at: float | None = None  # wall time of the last live frame
        self.offset: dict[str, float] = {}  # sid -> world time minus recording time
        self.media: dict[str, float] = {}  # sid -> last recording time

    def _live(self, wall: float) -> bool:
        return self.live_at is not None and wall - self.live_at <= self.live_keep_s

    def frame(self, sid: str, media: float | None, wall: float) -> tuple[float, bool]:
        """-> (the frame's time in this world, whether the stream went back in its recording)"""
        if media is None:
            self.live_at = wall
        if media is None or self._live(wall):
            self.t = max(self.t, wall)
            return wall, False
        last = self.media.get(sid)
        rewind = last is not None and media < last - JUMP_BACK_S
        if last is None or rewind or media > last + JUMP_AHEAD_S:
            self.offset[sid] = self.t - media
        self.media[sid] = media
        t = media + self.offset[sid]
        self.t = max(self.t, t)
        return t, rewind

    def tick(self, wall: float) -> float:
        """-> the world time now, for the sweeper: it follows the wall only while the world has a live stream"""
        if self._live(wall):
            self.t = max(self.t, wall)
        return self.t
