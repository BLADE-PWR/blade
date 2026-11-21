"""One BoT-SORT per stream, fed with ONNX detections (it needs the image for camera-motion compensation)."""
from __future__ import annotations

import numpy as np


class Dets:
    """The minimal 'Boxes' interface BOTSORT.update reads: conf, cls, xywh, boolean/int indexing."""

    def __init__(self, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray):
        self.xyxy, self.conf, self.cls = xyxy, conf, cls.astype(np.float32)
        self.xywh = (np.c_[(xyxy[:, :2] + xyxy[:, 2:]) / 2, xyxy[:, 2:] - xyxy[:, :2]]
                     if len(xyxy) else np.zeros((0, 4), np.float32))

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, i):
        return Dets(self.xyxy[i], self.conf[i], self.cls[i])


class StreamTracker:
    def __init__(self, tracker_args, first_tid: int = 0):
        """first_tid: ids start after it. BOTSORT numbers from 1, and the world takes a known (sid, tid) to be
        the same object, so a stream's next tracker (after a model switch) must not reuse its ids."""
        from ultralytics.trackers.bot_sort import BOTSORT  # imports torch; only the tracker code runs

        self.t = BOTSORT(tracker_args)
        self.first_tid = first_tid

    def update(self, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray, img: np.ndarray):
        """-> rows (x1, y1, x2, y2, tid, conf, cls, det_index) in the input's pixel space."""
        out = np.asarray(self.t.update(Dets(xyxy, conf, cls), img), dtype=np.float64).reshape(-1, 8)
        out[:, 4] += self.first_tid
        return out
