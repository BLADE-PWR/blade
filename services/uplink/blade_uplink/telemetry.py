"""The game's records on UDP :5005: tel indexed by frame for pairing; sim truth forwarded to the server's truth port."""
from __future__ import annotations

import json
import logging
import socket
import threading
import time

log = logging.getLogger("uplink.telemetry")
RESEND_REGISTRY_S = 10.0


class Telemetry:
    def __init__(self, listen: tuple[str, int], sid: str, mission: str, truth_to: tuple[str, int], wrap: int):
        self.sid, self.mission = sid, mission
        self.wrap = wrap
        self.tel: dict[int, dict] = {}  # frame % wrap -> tel record
        self.objects: dict[int, dict] = {}  # id -> spawn record, resent periodically
        self._parts: dict[int, dict[int, list]] = {}
        self.counts = {"tel": 0, "gt": 0, "spawn": 0, "evt": 0, "bad": 0}
        self.truth_to = truth_to
        self.out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.sock.bind(listen)
        self._stop = threading.Event()
        threading.Thread(target=self._run, name="telemetry", daemon=True).start()
        threading.Thread(target=self._resend, name="registry", daemon=True).start()

    def close(self) -> None:
        self._stop.set()
        self.sock.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                raw = self.sock.recv(65536)
            except OSError:
                return
            parts = raw.split(b"|", 2)
            if len(parts) != 3 or parts[0] != b"ZPI":
                self.counts["bad"] += 1
                continue
            kind = parts[1].decode(errors="replace")
            if kind not in ("tel", "gt", "spawn", "evt"):
                continue
            try:
                rec = json.loads(parts[2].replace(b"\\", b"/"))
            except ValueError:
                self.counts["bad"] += 1
                continue
            self.counts[kind] += 1
            if kind == "tel":
                self.tel[rec["frame"] % self.wrap] = rec
            elif kind == "spawn":
                self.objects[rec["id"]] = rec
                self._send("spawn", rec)
            elif kind == "gt":
                self._reassemble(rec)
            else:
                self._send("evt", rec)

    def _reassemble(self, rec: dict) -> None:
        """gt arrives split across datagrams; send a frame once every part is in."""
        frame = rec["frame"]
        held = self._parts.setdefault(frame, {})
        held[rec["part"]] = rec["objs"]
        if len(held) == rec["parts"]:
            objs = [row for k in sorted(held) for row in held[k]]
            del self._parts[frame]
            self._send("gt", {"frame": frame, "t_game": rec.get("t_game"), "objs": objs})
        if len(self._parts) > 64:  # drop frames that never completed
            for f in sorted(self._parts)[:-32]:
                del self._parts[f]

    def _send(self, kind: str, rec: dict) -> None:
        msg = json.dumps({"sid": self.sid, "wid": self.mission, "kind": kind, "t": time.time(), "rec": rec},
                         separators=(",", ":")).encode()
        try:
            self.out.sendto(msg, self.truth_to)
        except OSError as e:
            log.debug("truth send failed: %s", e)

    def _resend(self) -> None:
        """Resend the registry, so a server that started late learns every object id."""
        while not self._stop.wait(RESEND_REGISTRY_S):
            for rec in list(self.objects.values()):
                self._send("spawn", rec)
