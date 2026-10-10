"""One incoming MPEG-TS stream: bytes in, (frame, tel) pairs out, joined on PTS. Demux runs on its own thread."""
from __future__ import annotations

import io
import logging
import queue
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

import av
import numpy as np
from PIL import Image

from blade_common import klv

log = logging.getLogger("ingest.source")
# FFmpeg's H.264 parser holds each frame until the next starts (one frame of latency). The uplink
# writes one access unit per PES, so it is off.
DEMUX_OPTIONS = {"probesize": "1000000", "analyzeduration": "300000", "fflags": "+noparse+nofillin"}


class PipeReader(io.RawIOBase):
    """Blocking file-like over a queue of byte chunks; None in the queue = end of stream."""

    def __init__(self, maxsize: int = 256):
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize)
        self._buf = b""
        self._eof = False

    def readable(self) -> bool:
        return True

    def feed(self, chunk: bytes | None, block: bool = True, timeout: float | None = None) -> bool:
        try:
            self.q.put(chunk, block=block, timeout=timeout)
            return True
        except queue.Full:
            return False

    def read(self, n: int = -1) -> bytes:
        while not self._buf and not self._eof:
            chunk = self.q.get()
            if chunk is None:
                self._eof = True
                break
            self._buf = chunk
        if n is None or n < 0:
            n = len(self._buf)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out


class TelemetryTap:
    """KLV telemetry read straight from the TS packets (FFmpeg emits a metadata PES one frame late)."""
    DATA_IDS = (0xBD, 0xFC)

    def __init__(self, on_tel: Callable[[int, dict], None]):
        self.on_tel = on_tel
        self.rest = b""
        self.pes: dict[int, list] = {}  # pid -> [pts, payload, payload length]
        self.skip: set[int] = {0, 0x11, 0x1FFF}  # PAT, SDT, null; video PIDs get added

    def feed(self, data: bytes) -> None:
        b = self.rest + data
        i = b.find(b"\x47") if b[:1] != b"\x47" else 0
        if i < 0:
            self.rest = b""
            return
        end = i + (len(b) - i) // 188 * 188
        for o in range(i, end, 188):
            pkt = b[o:o + 188]
            if pkt[0] != 0x47:
                continue
            pid = (pkt[1] & 0x1F) << 8 | pkt[2]
            if pid in self.skip or not pkt[3] & 0x10:
                continue
            j = 4 + (1 + pkt[4] if pkt[3] & 0x20 else 0)
            if pkt[1] & 0x40:
                self.pes.pop(pid, None)
                if pkt[j:j + 3] != b"\x00\x00\x01" or pkt[j + 3] not in self.DATA_IDS:
                    self.skip.add(pid)
                    continue
                length = pkt[j + 4] << 8 | pkt[j + 5]
                hl = pkt[j + 8]
                if not pkt[j + 7] & 0x80 or not length:
                    continue  # no PTS, or no length: leave it to the demuxer
                q = pkt[j + 9:j + 14]
                pts = ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1
                self.pes[pid] = [pts, bytearray(pkt[j + 9 + hl:]), length - 3 - hl]
            elif pid in self.pes:
                self.pes[pid][1] += pkt[j:]
            else:
                continue
            st = self.pes[pid]
            if len(st[1]) >= st[2]:
                del self.pes[pid]
                tel = klv.decode_tel(bytes(st[1][:st[2]]))
                if tel is not None:
                    self.on_tel(st[0], tel)
        self.rest = b[end:]


@dataclass
class Paired:
    sid: str
    tel: dict
    jpeg: bytes
    jpeg_size: tuple[int, int]
    t_in: float
    t_out: float


@dataclass
class Source:
    key: str  # "udp:<ip>:<port>", "tcp:..." or "push:<sid>"
    kind: str  # live | replay
    sid_override: str | None = None
    delivery: str = "latest"
    wid: str | None = None
    jpeg_w: int = 1280
    jpeg_h: int = 720
    jpeg_quality: int = 85
    on_frame: Callable[[Paired], None] | None = None
    on_sid: Callable[[Source], None] | None = None
    pipe: PipeReader = field(default_factory=PipeReader)
    sid: str | None = None
    started: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    frames: int = 0
    unpaired: int = 0
    decode_errors: int = 0
    video: str | None = None
    size: tuple[int, int] | None = None
    service_name: str | None = None
    done: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self):
        self.sid = self.sid_override
        self._tel: OrderedDict[int, dict] = OrderedDict()  # pts -> tel
        self._pending: deque = deque()  # (pts, VideoFrame, t_in) waiting for tel
        self.tap = TelemetryTap(self._tap_tel)
        self.thread = threading.Thread(target=self._run, name=f"demux-{self.key}", daemon=True)
        self.thread.start()

    def feed(self, chunk: bytes, block: bool = False) -> bool:
        self.last_seen = time.time()
        try:
            self.tap.feed(chunk)
        except Exception:
            log.exception("%s: telemetry tap", self.key)
        return self.pipe.feed(chunk, block=block)

    def _tap_tel(self, pts: int, tel: dict) -> None:
        self._tel[pts] = tel
        if self.sid is None:
            self._set_sid(tel)

    def close(self) -> None:
        self.pipe.feed(None, block=True, timeout=5)

    def _run(self) -> None:
        try:
            container = av.open(self.pipe, format="mpegts", mode="r", options=DEMUX_OPTIONS)
        except Exception as e:
            log.warning("%s: not an MPEG-TS stream (%s)", self.key, e)
            self.done.set()
            return
        self.service_name = (container.metadata or {}).get("service_name")
        vstreams = [s for s in container.streams if s.type == "video"]
        if not vstreams:
            log.warning("%s: no video stream", self.key)
            self.done.set()
            return
        v = vstreams[0]
        v.thread_type = "NONE"  # frame threads add a frame of delay each
        self.video = v.codec_context.name
        log.info("%s: %s video, %d data stream(s)", self.key, self.video,
                 sum(s.type == "data" for s in container.streams))
        try:
            for pkt in container.demux():
                if pkt.stream.type == "data":
                    if pkt.size and pkt.pts is not None:
                        tel = klv.decode_tel(bytes(pkt))
                        if tel is not None:
                            self._add_tel(pkt.pts, tel)
                elif pkt.stream.index == v.index:
                    t_in = time.time()
                    try:
                        frames = pkt.decode()
                    except av.error.FFmpegError:
                        self.decode_errors += 1
                        continue
                    for frame in frames:
                        self._pending.append((frame.pts, frame, t_in))
                    self._match()
            try:
                for frame in v.decode(None):
                    self._pending.append((frame.pts, frame, time.time()))
            except av.error.FFmpegError:
                pass
            while self._pending:
                n = len(self._pending)
                self._match()
                if len(self._pending) == n:  # no telemetry for the head frame: drop it
                    self._pending.popleft()
                    self.unpaired += 1
        except Exception:
            log.exception("%s: demux stopped", self.key)
        finally:
            container.close()
            self.done.set()
            log.info("%s (%s): ended after %d frames, %d unpaired, %d decode errors",
                     self.key, self.sid, self.frames, self.unpaired, self.decode_errors)

    def _set_sid(self, tel: dict) -> None:
        self.sid = tel.get("sid") or self.service_name or self.key.replace(":", "-")
        if self.on_sid:
            self.on_sid(self)

    def _add_tel(self, pts: int, tel: dict) -> None:
        """The demuxer's copy of a tel packet: late, or the only one if the tap could not read it."""
        self._tel.setdefault(pts, tel)
        while len(self._tel) > 240:
            self._tel.popitem(last=False)
        if self.sid is None:
            self._set_sid(tel)
        self._match()

    def _match(self) -> None:
        while self._pending:
            pts, frame, t_in = self._pending[0]
            tel = self._tel.pop(pts, None)
            if tel is None:
                if len(self._pending) <= 8:  # give a late tel packet a few frames
                    return
                self._pending.popleft()
                self.unpaired += 1
                continue
            self._pending.popleft()
            self._emit(frame, tel, t_in)

    def _emit(self, frame: av.VideoFrame, tel: dict, t_in: float) -> None:
        self.size = (frame.width, frame.height)
        rgb = frame.reformat(width=self.jpeg_w, height=self.jpeg_h, format="rgb24").to_ndarray()
        buf = io.BytesIO()
        Image.fromarray(np.ascontiguousarray(rgb)).save(buf, "JPEG", quality=self.jpeg_quality)
        self.frames += 1
        if self.sid_override:
            tel["sid"] = self.sid_override
        if self.on_frame:
            self.on_frame(Paired(self.sid or "?", tel, buf.getvalue(), (self.jpeg_w, self.jpeg_h), t_in, time.time()))

    def info(self) -> dict:
        age = max(time.time() - self.started, 1e-6)
        return {"sid": self.sid, "key": self.key, "kind": self.kind, "wid": self.wid, "video": self.video,
                "size": self.size, "frames": self.frames, "fps_avg": round(self.frames / age, 1),
                "unpaired": self.unpaired, "decode_errors": self.decode_errors,
                "started": self.started, "last_seen": self.last_seen, "service_name": self.service_name,
                "alive": not self.done.is_set()}
