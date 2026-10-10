"""How the MPEG-TS leaves the Arma PC: file-like sinks for PyAV's muxer; TsWriter calls end_frame() after
each frame. udp: as many TS packets per datagram as fit the MTU. tcp: a short frame queue; past
max_queue_s of video it is emptied and frames are dropped up to the next keyframe. Reconnects by itself."""
from __future__ import annotations

import collections
import contextlib
import logging
import socket
import threading
import time

log = logging.getLogger("uplink.transport")
TS_PACKET = 188
IP_UDP_HEADERS = 28


class UdpSink:
    def __init__(self, host: str, port: int, mtu: int = 1280):
        self.addr = (host, port)
        self.per_datagram = max(1, (mtu - IP_UDP_HEADERS) // TS_PACKET)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
        self.bytes = 0
        self._pending = b""

    def write(self, data) -> int:
        b = self._pending + bytes(data)
        n = len(b) - len(b) % TS_PACKET
        step = self.per_datagram * TS_PACKET
        for i in range(0, n, step):
            try:
                self.sock.sendto(b[i:min(i + step, n)], self.addr)
            except OSError as e:  # server down: keep going, like a drone would
                log.debug("send failed: %s", e)
        self._pending = b[n:]
        self.bytes += len(data)
        return len(data)

    def end_frame(self, keyframe: bool) -> None:
        pass

    def stats(self) -> str:
        return f"udp {self.per_datagram} TS/datagram"

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.sock.close()


class TcpSink:
    def __init__(self, host: str, port: int, fps: int, max_queue_s: float = 0.5, connect=None):
        self.addr = (host, port)
        self.max_frames = max(2, int(fps * max_queue_s))
        self.bytes = 0
        self.frames_dropped = 0
        self.resyncs = 0
        self._unit = bytearray()
        self._queue: collections.deque[tuple[bytes, bool]] = collections.deque()
        self._cv = threading.Condition()
        self._skip_to_key = True
        self._closed = False
        self._connect = connect or self._tcp_connect
        self.connected = False
        threading.Thread(target=self._run, name="tcp-uplink", daemon=True).start()

    def write(self, data) -> int:
        self._unit += data
        self.bytes += len(data)
        return len(data)

    def end_frame(self, keyframe: bool) -> None:
        unit, self._unit = bytes(self._unit), bytearray()
        with self._cv:
            if self._skip_to_key and not keyframe:
                self.frames_dropped += 1
                return
            self._skip_to_key = False
            if len(self._queue) >= self.max_frames:  # the link is behind: restart from a keyframe
                self._resync()
                self.resyncs += 1
                if not keyframe:
                    self.frames_dropped += 1
                    return
                self._skip_to_key = False
            self._queue.append((unit, keyframe))
            self._cv.notify()

    def stats(self) -> str:
        with self._cv:
            q = len(self._queue)
        state = "connected" if self.connected else "connecting"
        return f"tcp {state}, queue {q} frames, {self.frames_dropped} frames dropped, {self.resyncs} resyncs"

    def flush(self) -> None:
        pass

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify()

    def _resync(self) -> None:
        """Drop everything queued and wait for the next keyframe (caller holds the lock)."""
        self.frames_dropped += len(self._queue)
        self._queue.clear()
        self._skip_to_key = True

    def _tcp_connect(self):
        s = socket.create_connection(self.addr, timeout=3)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # a small kernel buffer: backlog then shows in our queue, where it can be dropped frame by frame
        s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
        s.settimeout(5)
        return s

    def _run(self) -> None:
        sock = None
        while True:
            with self._cv:
                while not self._queue and not self._closed:
                    self._cv.wait(0.5)
                if self._closed and not self._queue:
                    break
                unit = self._queue.popleft() if self._queue and sock is not None else None
            if sock is None:
                try:
                    sock = self._connect()
                    self.connected = True
                    log.info("tcp connected to %s:%d", *self.addr)
                except OSError as e:
                    log.debug("tcp connect: %s", e)
                    with self._cv:
                        self._resync()
                    time.sleep(1.0)
                continue
            try:
                sock.sendall(unit[0])
            except OSError as e:
                log.warning("tcp link lost (%s); reconnecting", e)
                self.connected = False
                with contextlib.suppress(OSError):
                    sock.close()
                sock = None
                with self._cv:
                    self._resync()
        if sock is not None:
            sock.close()
