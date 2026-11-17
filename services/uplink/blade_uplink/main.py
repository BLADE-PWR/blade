"""uplink: runs on the Arma PC and turns the game into a drone stream: screen -> frame-number bar -> pair
with tel from UDP :5005 -> H.264 + tel (same PTS) -> MPEG-TS to ingest; sim truth -> the truth port."""
from __future__ import annotations

import argparse
import logging
import signal
import time

import frame_code
from blade_common import ports
from blade_common.service import setup_logging

from .encoder import TsWriter
from .telemetry import Telemetry
from .transport import TcpSink, UdpSink

log = logging.getLogger("uplink")


TEL_LISTEN = ("127.0.0.1", 5005)  # where the zpi_telemetry extension sends
UDP_MTU = 1280  # Tailscale/VPN-safe; costs one TS packet per datagram on a plain LAN
TCP_MAX_QUEUE_S = 0.5  # seconds of video that may wait before skipping to the next keyframe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sid", required=True, help="stream id, e.g. pc1-uav1")
    ap.add_argument("--server", required=True, help="ingest host")
    ap.add_argument("--transport", choices=["udp", "tcp"], default="udp",
                    help="udp: lowest latency on a good LAN; tcp: resends lost packets (internet, Wi-Fi)")
    ap.add_argument("--fps", type=int, default=60, help="stream frame rate")
    ap.add_argument("--bitrate", default="8M")
    ap.add_argument("--bar-bits", type=int, default=12, help="data squares in the frame bar")
    ap.add_argument("--bar-size", type=int, default=4, help="bar square side, screen px")
    args = ap.parse_args(argv)
    setup_logging()

    fps = args.fps
    bar = frame_code.Bar(0, 0, args.bar_size, args.bar_bits)
    tel = Telemetry(TEL_LISTEN, args.sid, (args.server, ports.TRUTH_UDP), wrap=bar.wrap)
    import dxcam  # Windows only (DXGI desktop duplication), so imported here and not at the top

    screen = dxcam.create(output_idx=0, output_color="BGR")  # monitor 0, whole screen

    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if args.transport == "tcp":
        sink = TcpSink(args.server, ports.VIDEO_UDP, fps, TCP_MAX_QUEUE_S)
    else:
        sink = UdpSink(args.server, ports.VIDEO_UDP, UDP_MTU)
    writer: TsWriter | None = None
    period, next_grab = 1.0 / fps, 0.0
    last = None
    zero = {"paired": 0, "dup": 0, "unreadable": 0, "no_tel": 0, "enc_ms": 0.0}
    st = dict(zero)
    t_report = time.time()
    log.info("uplink %s -> %s:%d (video, %s), :%d (truth); telemetry on %s:%d",
             args.sid, args.server, ports.VIDEO_UDP, args.transport, ports.TRUTH_UDP, *TEL_LISTEN)
    try:
        while not stop:
            now = time.time()
            if now - t_report >= 5:
                dt = now - t_report
                log.info("paired %.1f fps | dup %d unreadable %d no-tel %d | encode %.1f ms | %.1f Mbit/s | %s"
                         " | tel %s",
                         st["paired"] / dt, st["dup"], st["unreadable"], st["no_tel"],
                         st["enc_ms"] / max(st["paired"], 1), sink.bytes * 8 / dt / 1e6, sink.stats(), tel.counts)
                st = dict(zero)
                sink.bytes = 0
                t_report = now

            if now < next_grab:
                time.sleep(next_grab - now)
                continue
            image = screen.grab()
            if image is None:
                time.sleep(0.001)
                continue
            t_capture = time.time()
            number = frame_code.decode(image, bar)
            if number is None:
                st["unreadable"] += 1
                continue
            if number == last:  # the same game frame grabbed twice
                st["dup"] += 1
                continue
            last = number
            record = tel.tel.get(number)
            if record is None:
                st["no_tel"] += 1
                continue
            image = frame_code.strip(image, bar)
            if writer is None:
                writer = TsWriter(sink, args.sid, image.shape[1], image.shape[0], fps, args.bitrate)
            record = dict(record, sid=args.sid, t_capture=round(t_capture, 6))
            t0 = time.perf_counter()
            writer.write(image, record, t_capture)
            st["enc_ms"] += (time.perf_counter() - t0) * 1000
            st["paired"] += 1
            next_grab = max(next_grab + period, now)
    finally:
        if writer:
            writer.close()
        else:
            sink.close()
        screen.release()
        tel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
