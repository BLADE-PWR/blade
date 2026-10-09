"""H.264 + one KLV telemetry packet per frame (same PTS) in one MPEG-TS stream. No B-frames, an IDR every second."""
from __future__ import annotations

import fractions
import logging

import av
import numpy as np

from blade_common import klv

log = logging.getLogger("uplink.encoder")
TB = fractions.Fraction(1, 90000)


def pick_encoder(name: str) -> str:
    if name != "auto":
        return name
    for codec in ("h264_nvenc", "libx264"):
        try:
            c = av.CodecContext.create(codec, "w")
            c.width, c.height, c.pix_fmt, c.time_base = 256, 256, "yuv420p", TB  # NVENC refuses 64x64
            c.open()
            return codec
        except Exception:
            continue
    raise RuntimeError("no H.264 encoder available (h264_nvenc, libx264)")


class TsWriter:
    def __init__(self, sink, sid: str, width: int, height: int, fps: int = 60,
                 bitrate: str = "8M", encoder: str = "auto"):
        self.sink = sink
        # write each packet at once, and set the PES length so the receiver need not wait for the next PES
        self.out = av.open(sink, "w", format="mpegts",
                           options={"flush_packets": "1", "omit_video_pes_length": "0"})
        self.out.metadata["service_name"] = sid
        self.out.metadata["service_provider"] = "blade"
        codec = pick_encoder(encoder)
        v = self.out.add_stream(codec, rate=fps)
        v.width, v.height, v.pix_fmt = width, height, "yuv420p"
        v.time_base = TB
        v.codec_context.time_base = TB  # else PTS are rounded to 1/fps and capture jitter collides them
        v.bit_rate = _rate(bitrate)
        if codec == "h264_nvenc":
            v.options = {"preset": "p1", "tune": "ull", "zerolatency": "1", "bf": "0",
                         "g": str(fps), "rc": "cbr", "delay": "0"}
        else:
            v.options = {"preset": "ultrafast", "tune": "zerolatency", "bf": "0", "g": str(fps),
                         "x264-params": f"keyint={fps}:min-keyint={fps}:scenecut=0"}
        self.v = v
        self.d = self.out.add_data_stream("klv")
        self.d.time_base = TB
        self.t0: float | None = None
        self.last_pts = 0  # first PTS is 1, so the first telemetry DTS (PTS - 1) is not negative
        log.info("encoder %s, %dx%d @ %d fps, %s", codec, width, height, fps, bitrate)

    def write(self, image_bgr: np.ndarray, tel: dict, t_capture: float) -> None:
        if self.t0 is None:
            self.t0 = t_capture
        pts = max(int(round((t_capture - self.t0) * 90000)), self.last_pts + 1)
        # FFmpeg's interleaver releases a packet only while every stream has one queued. Writing
        # tel (DTS N-1), video (DTS N), then a tiny pad (PTS N+1) makes all of frame N leave in this
        # call; otherwise tel or video waits for the next frame.
        self._data(klv.encode_tel(tel), pts, dts=pts - 1)
        frame = av.VideoFrame.from_ndarray(image_bgr, format="bgr24")
        frame.pts, frame.time_base = pts, TB
        keyframe = False
        for pkt in self.v.encode(frame):
            keyframe |= pkt.is_keyframe
            self.out.mux(pkt)
        self._data(klv.encode(klv.PAD_KEY, b""), pts + 1)
        self.last_pts = pts + 1
        end = getattr(self.sink, "end_frame", None)
        if end:
            end(keyframe)

    def _data(self, payload: bytes, pts: int, dts: int | None = None) -> None:
        p = av.Packet(payload)
        p.stream, p.pts, p.dts, p.time_base = self.d, pts, pts if dts is None else dts, TB
        self.out.mux(p)

    def close(self) -> None:
        try:
            for pkt in self.v.encode():
                self.out.mux(pkt)
            self.out.close()
        finally:
            self.sink.close()


def _rate(s: str) -> int:
    s = s.strip().upper()
    mult = {"K": 1_000, "M": 1_000_000}.get(s[-1], 1)
    return int(float(s[:-1] if s[-1] in "KM" else s) * mult)
