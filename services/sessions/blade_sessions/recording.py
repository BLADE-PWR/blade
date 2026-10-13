"""Recording format: recordings/<id>/ holds manifest.json, stream.ts (the MPEG-TS as it arrived),
index.jsonl ({"o": offset, "n": bytes, "t": receive time} per chunk, which replays pace on) and
truth.jsonl ({"kind", "t", "rec"})."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import av

from blade_common import klv

log = logging.getLogger("sessions.recording")
SCHEMA_V = 1


def scan_ts(ts: Path) -> dict:
    """Read a .ts without decoding: frame range, image size, scene, fps, duration, and a PTS index."""
    info: dict = {"frames": 0, "frame_first": None, "frame_last": None, "image": None, "scene": None,
                  "seed": None, "sid": None}
    index, pts0, pts_last = [], None, None
    with av.open(str(ts)) as c:
        v = next((s for s in c.streams if s.type == "video"), None)
        if v is None:
            raise ValueError(f"{ts}: no video stream")
        info["codec"] = v.codec_context.name
        for pkt in c.demux():
            if pkt.pts is None:
                continue
            if pkt.stream.type == "data" and pkt.size:
                tel = klv.decode_tel(bytes(pkt))
                if tel:
                    info["frame_first"] = info["frame_first"] if info["frame_first"] is not None else tel["frame"]
                    info["frame_last"] = tel["frame"]
                    info["image"] = info["image"] or tel.get("image")
                    info["scene"] = info["scene"] or tel.get("scene")
                    info["seed"] = info["seed"] if info["seed"] is not None else tel.get("seed")
                    info["sid"] = info["sid"] or tel.get("sid")
            elif pkt.stream.index == v.index:
                info["frames"] += 1
                pts0 = pkt.pts if pts0 is None else pts0
                pts_last = pkt.pts
                if pkt.pos is not None and pkt.pos >= 0:
                    index.append((pkt.pos, (pkt.pts - pts0) / 90000.0))
    dur = (pts_last - pts0) / 90000.0 if pts0 is not None else 0.0
    info["duration_s"] = round(dur, 3)
    info["fps"] = round((info["frames"] - 1) / dur, 2) if dur > 0 else None
    info["_pts_index"] = index
    return info


def _pes_key(pkt: bytes) -> bytes | None:
    """The KLV key (first 16 payload bytes) of a TS packet that starts a PES."""
    i = 4 + (1 + pkt[4] if pkt[3] & 0x20 else 0)  # skip the adaptation field
    if pkt[i:i + 3] != b"\x00\x00\x01":
        return None
    i += 9 + pkt[i + 8]  # skip the PES header
    return pkt[i:i + 16]


def frame_ends(ts: Path) -> list[int]:
    """Byte offset where each frame is complete (after its pad packet, or its tel in older recordings)."""
    with av.open(str(ts)) as c:
        pids = [s.id for s in c.streams if s.type == "data"]
    if not pids:
        raise ValueError(f"{ts}: no telemetry stream")
    pid = pids[0]
    ends: dict[bytes, list[int]] = {klv.TEL_KEY: [], klv.PAD_KEY: []}
    key, last = None, None
    with open(ts, "rb") as f:
        off = 0
        while True:
            pkt = f.read(188)
            if len(pkt) < 188:
                break
            if pkt[0] == 0x47 and ((pkt[1] & 0x1F) << 8 | pkt[2]) == pid:
                if pkt[1] & 0x40:  # a new PES: the previous one ended at last
                    if key in ends:
                        ends[key].append(last)
                    key = _pes_key(pkt)
                last = off + 188
            off += 188
    if key in ends:
        ends[key].append(last)
    return ends[klv.PAD_KEY] or ends[klv.TEL_KEY]


def write_index_from_pts(rec_dir: Path, index: list[tuple[int, float]], size: int) -> None:
    """index.jsonl for an imported recording: chunks between video packets, timed by PTS."""
    with open(rec_dir / "index.jsonl", "w") as f:
        offs = [0] + [o for o, _ in index[1:]] + [size]
        times = [t for _, t in index] or [0.0]
        for k in range(len(offs) - 1):
            f.write(json.dumps({"o": offs[k], "n": offs[k + 1] - offs[k], "t": times[min(k, len(times) - 1)]}) + "\n")


def finalize(rec_dir: Path, extra: dict | None = None) -> dict:
    """Write (or rewrite) manifest.json from what is in the folder."""
    ts = rec_dir / "stream.ts"
    info = scan_ts(ts)
    pts_index = info.pop("_pts_index")
    if not (rec_dir / "index.jsonl").exists():
        write_index_from_pts(rec_dir, pts_index, ts.stat().st_size)
    m = rec_dir / "manifest.json"
    old = json.loads(m.read_text()) if m.exists() else {}
    manifest = {"v": SCHEMA_V, "id": rec_dir.name, "source": "import", **old, **(extra or {}), **info,
                "bytes": ts.stat().st_size, "has_truth": (rec_dir / "truth.jsonl").exists(),
                "finalized": time.time()}
    m.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def _jsonl(p: Path) -> list[dict]:
    with open(p) as f:
        return [json.loads(line) for line in f if line.strip()]


def load_index(rec_dir: Path) -> list[dict]:
    return _jsonl(rec_dir / "index.jsonl")


def load_truth(rec_dir: Path) -> list[dict]:
    p = rec_dir / "truth.jsonl"
    return _jsonl(p) if p.exists() else []


def list_recordings(root: Path) -> list[dict]:
    out = []
    for d in sorted(root.iterdir() if root.exists() else []):
        m = d / "manifest.json"
        if d.is_dir() and m.exists():
            try:
                rec = json.loads(m.read_text())
                if not rec.get("broken"):
                    out.append(rec)
            except ValueError:
                log.warning("%s: unreadable manifest", m)
    return out
