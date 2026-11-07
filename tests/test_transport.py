import socket
import threading
import time

from conftest import frame_image

from blade_ingest.source import Source
from blade_uplink.encoder import TsWriter
from blade_uplink.transport import TcpSink, UdpSink


class SlowSocket:
    """sendall blocks while the gate is closed: a link that cannot keep up"""

    def __init__(self):
        self.gate = threading.Event()
        self.sent: list[bytes] = []

    def sendall(self, b):
        self.gate.wait()
        self.sent.append(bytes(b))

    def close(self):
        pass


def test_tcp_queue_skips_to_the_next_keyframe():
    fake = SlowSocket()
    sink = TcpSink("x", 0, fps=10, max_queue_s=0.5, connect=lambda: fake)  # 5 frames may wait
    for k in range(40):  # keyframe every 10 frames, link stalled
        sink.write(f"f{k:02d}|".encode())
        sink.end_frame(keyframe=k % 10 == 0)
    time.sleep(0.2)
    fake.gate.set()
    time.sleep(0.3)
    got = [s.decode() for s in fake.sent]
    assert sink.resyncs >= 1 and sink.frames_dropped > 0
    # whatever was sent is in order, and after the first frame every jump lands on a keyframe
    nums = [int(s[1:3]) for s in got]
    assert nums == sorted(nums)
    for a, b in zip(nums[1:], nums[2:]):
        assert b == a + 1 or b % 10 == 0
    sink.close()


def test_tcp_stream_into_ingest_pairs_every_frame():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    got = []
    src = Source(key="tcp-test", kind="live", jpeg_w=160, jpeg_h=90, on_frame=got.append)

    def serve():
        conn, _ = srv.accept()
        while data := conn.recv(65536):
            src.feed(data, block=True)
        src.close()

    threading.Thread(target=serve, daemon=True).start()
    sink = TcpSink("127.0.0.1", port, fps=30)
    w = TsWriter(sink, "tcp-uav", 320, 180, fps=30, bitrate="1M", encoder="libx264")
    t0 = time.time()
    for k in range(60):
        tel = {"frame": 2000 + k, "sid": "tcp-uav", "image": {"w": 320, "h": 180}}
        w.write(frame_image(k, 320, 180), tel, t0 + k / 30)
        time.sleep(0.005)
    time.sleep(0.5)
    w.close()
    assert src.done.wait(10)
    frames = [p.tel["frame"] for p in got]
    assert frames[0] == 2000 and frames == list(range(2000, 2000 + len(frames))) and len(frames) >= 59


def test_udp_datagrams_fit_the_mtu():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(0.3)
    for mtu, per in ((1280, 6), (1500, 7)):
        sink = UdpSink("127.0.0.1", rx.getsockname()[1], mtu=mtu)
        sink.write(bytes(188 * 20))
        sizes = []
        try:
            while True:
                sizes.append(len(rx.recv(4096)))
        except TimeoutError:
            pass
        assert sum(sizes) == 20 * 188 and max(sizes) == per * 188 and max(sizes) + 28 <= mtu
        sink.close()


def test_tcp_reconnects_and_restarts_on_a_keyframe():
    attempts = []
    fake = SlowSocket()
    fake.gate.set()

    def connect():
        attempts.append(time.time())
        if len(attempts) == 1:
            raise ConnectionRefusedError("server not up yet")
        return fake

    sink = TcpSink("x", 0, fps=10, connect=connect)
    deadline = time.time() + 3
    k = 0
    while time.time() < deadline and not fake.sent:
        sink.write(f"f{k:02d}|".encode())
        sink.end_frame(keyframe=k % 5 == 0)
        k += 1
        time.sleep(0.05)
    time.sleep(0.2)
    assert len(attempts) >= 2 and fake.sent
    assert int(fake.sent[0].decode()[1:3]) % 5 == 0  # the first frame after connecting is a keyframe
    sink.close()
