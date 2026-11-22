#!/usr/bin/env python3
"""nats-server + every service in one terminal, without Docker. Ctrl+C stops all."""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ["ingest", "sessions", "detector", "georef", "world", "evaluator", "decision", "terrain", "gateway"]
COLORS = ["31", "32", "33", "34", "35", "36", "91", "92"]


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def pump(name: str, color: str, stream) -> None:
    for line in iter(stream.readline, b""):
        text = line.decode(errors="replace").rstrip()
        if "objc[" in text and "implemented in both" in text:
            continue  # harmless macOS warning: PyAV and OpenCV both bundle libavdevice
        print(f"\033[{color}m{name:>9}\033[0m | {text}", flush=True)


# Children get their own process group, so Ctrl+C reaches only this script, which then stops them in order:
# services first, NATS last (start_new_session does that on Linux and macOS, but Windows ignores it).
OWN_GROUP = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default="arma_detector_v1@1", help="model id, or 'oracle'")
    ap.add_argument("--only", help="comma-separated subset of: " + ",".join(SERVICES))
    ap.add_argument("--no-record", action="store_true", help="do not record live streams by default")
    ap.add_argument("--detectors", type=int, default=1, help="detector replicas (streams are split between them)")
    ap.add_argument("--detect-fps", type=float, default=0, help="live streams: detect at most N frames/s (0 = all)")
    args = ap.parse_args()

    env = dict(os.environ, MAPS_DIR=str(ROOT / "maps"), MODEL=args.model,
               MODELS_DIR=str(ROOT / "models"), RECORDINGS_DIR=str(ROOT / "recordings"),
               EVAL_DIR=str(ROOT / "state" / "eval"),
               RECORD_ALL="0" if args.no_record else "1", MAX_FPS=str(args.detect_fps), PYTHONUNBUFFERED="1")
    wanted = args.only.split(",") if args.only else SERVICES
    if "georef" in wanted and not any((ROOT / "maps").glob("*/meta.json")):
        print("no map package in maps/ (georef needs one): see the README, Map package")
        return 1
    procs: list[tuple[str, subprocess.Popen]] = []
    if not port_open(4222):
        nats = shutil.which("nats-server")
        if not nats:
            print("nats-server not found (macOS: brew install nats-server) and nothing listens on :4222")
            return 1
        store = ROOT / "state" / "nats"
        store.mkdir(parents=True, exist_ok=True)
        conf = ROOT / "state" / "nats.conf"  # the compose config with a local JetStream folder
        conf.write_text((ROOT / "deploy" / "nats.conf").read_text().replace("/data/jetstream", store.as_posix()))
        procs.append(("nats", subprocess.Popen([nats, "-c", str(conf)], **OWN_GROUP,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)))
        for _ in range(50):
            if port_open(4222):
                break
            time.sleep(0.1)
    for name in wanted:
        exe = Path(sys.executable).parent / f"blade-{name}"
        copies = args.detectors if name == "detector" else 1
        for k in range(copies):
            label, extra = name, {}
            if copies > 1:
                label = f"{name}{k + 1}"
                extra = {"INSTANCE": label, "PORT": str(8003 if k == 0 else 8100 + k)}
            procs.append((label, subprocess.Popen([str(exe)], env={**env, **extra}, cwd=ROOT, **OWN_GROUP,
                                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)))
    for i, (name, p) in enumerate(procs):
        threading.Thread(target=pump, args=(name, COLORS[i % len(COLORS)], p.stdout), daemon=True).start()

    def stop(*_):
        for _, p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)  # a clean shutdown
                try:
                    p.wait(8)
                except subprocess.TimeoutExpired:
                    p.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(1)
        for name, p in procs:
            if p.poll() is not None:
                print(f"{name} exited with {p.returncode}; stopping everything")
                stop()


if __name__ == "__main__":
    raise SystemExit(main())
