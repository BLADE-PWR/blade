"""Import the dataset generator's pictures (images/ + meta/) as a "stills" recording: one picture per
second with its exact telemetry, and the labelled objects as truth."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np
from PIL import Image

from blade_uplink.encoder import TsWriter

from .recording import finalize

TRUTH_CLS = {"soldier": "soldier", "tank": "tank", "military_vehicle": "wheeled_vehicle"}
SID = "stills"
STANCE = {"standing": "STAND", "kneeling": "CROUCH", "prone": "PRONE"}


def tel_from_meta(m: dict, frame: int, sid: str, size: tuple[int, int]) -> dict | None:
    c = m.get("camera") or {}
    if not (c.get("pos_asl") and c.get("forward_axis") and c.get("hfov_deg") and c.get("vfov_deg")):
        return None
    laser = m.get("laser_rangefinder") or {}
    rng = laser.get("range") if isinstance(laser, dict) else None
    return {"frame": frame, "t_game": m.get("t_game"), "sid": sid,
            "camera": {"pos_asl": c["pos_asl"], "x_axis": c["right_axis"], "y_axis": c["up_axis"],
                       "z_axis": c["forward_axis"], "hfov_deg": c["hfov_deg"], "vfov_deg": c["vfov_deg"]},
            "range_m": float(rng) if rng not in (None, -1) else None, "image": {"w": size[0], "h": size[1]},
            "scene": m.get("scene"), "seed": m.get("seed"), "region": m.get("region"),
            "source_image": m.get("image"), "source_frame": m.get("frame")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dataset", type=Path, help="folder with meta/ and images/")
    ap.add_argument("--out", type=Path, required=True, help="new recording folder, e.g. recordings/stills_val")
    ap.add_argument("--split", help="only this split (train / val / test)")
    args = ap.parse_args(argv)

    metas = sorted((args.dataset / "meta").glob("*.json"))
    if not metas:
        raise SystemExit(f"no meta/*.json in {args.dataset}")
    if args.out.exists():
        raise SystemExit(f"{args.out} exists")
    args.out.mkdir(parents=True)
    writer, size, n, skipped = None, None, 0, 0
    sessions: dict[str, int] = {}
    truth = open(args.out / "truth.jsonl", "w")
    spawned: set[int] = set()
    t0 = time.time()
    for mp in metas:
        m = json.loads(mp.read_text())
        if args.split and f"/{args.split}/" not in m.get("image", ""):
            continue
        img_path = args.dataset / m["image"]
        if not img_path.exists():
            skipped += 1
            continue
        im = Image.open(img_path).convert("RGB")
        if size is None:
            size = im.size
            writer = TsWriter(open(args.out / "stream.ts", "wb"), SID, size[0], size[1], fps=1,
                              bitrate="12M", encoder="auto")
        if im.size != size:
            im = im.resize(size, Image.Resampling.BILINEAR)
        tel = tel_from_meta(m, 1000 + n, SID, size)
        if tel is None:
            skipped += 1
            continue
        base = 100000 * sessions.setdefault(m.get("session") or "?", len(sessions))  # ids are per game session
        rows = []
        for o in m.get("objects", []):
            kind = TRUTH_CLS.get(o.get("type", ""))
            pos = o.get("ground_pos_asl") or o.get("world_pos_asl")
            if not kind or not pos or o.get("object_id") is None:
                continue
            oid = base + int(o["object_id"])
            if oid not in spawned:
                spawned.add(oid)
                truth.write(json.dumps({"kind": "spawn", "t": 0.0, "rec": {"id": oid, "cls": kind,
                                                                         "frame": tel["frame"]}}) + "\n")
            rows.append([oid, pos, 0.0, [0, 0, 0], STANCE.get(o.get("stance", ""), "")])
        truth.write(json.dumps({"kind": "gt", "t": float(n), "rec": {"frame": tel["frame"], "t_game": m.get("t_game"),
                                                                     "objs": rows}}) + "\n")
        bgr = np.ascontiguousarray(np.asarray(im)[:, :, ::-1])
        writer.write(bgr, tel, t0 + n)
        n += 1
    truth.close()
    if writer is None:
        raise SystemExit("no usable pictures")
    writer.close()
    m = finalize(args.out, {"source": "stills", "sid": SID, "pictures": n,
                            "dataset": str(args.dataset.resolve())})
    print(f"{args.out}: {n} pictures ({skipped} skipped), {m['bytes'] / 1e6:.1f} MB, {len(spawned)} objects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
