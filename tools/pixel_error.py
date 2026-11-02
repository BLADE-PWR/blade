#!/usr/bin/env python3
"""Detector anchor error in pixels, against sim truth projected into the image.
    uv run python tools/pixel_error.py recordings/<id> [--step 10]"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import av
import numpy as np

from blade_common import klv
from blade_detector.package import ModelPackage, detect_batch
from blade_geometry import Camera
from blade_georef.project import anchor


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("recording", type=Path)
    ap.add_argument("--model", type=Path, default=Path("models/arma_detector_v1"))
    ap.add_argument("--step", type=int, default=10, help="run the detector on every Nth frame")
    args = ap.parse_args()

    pkg = ModelPackage.load(args.model)
    gt = {}
    for line in open(args.recording / "truth.jsonl"):
        r = json.loads(line)
        if r["kind"] == "gt":
            gt[r["rec"]["frame"]] = r["rec"]["objs"]
    gt_frames = np.array(sorted(gt))

    err = defaultdict(list)  # cls -> [(dx, dy, box_h)]
    tel, n = None, 0
    with av.open(str(args.recording / "stream.ts")) as c:
        for pkt in c.demux():
            if pkt.stream.type == "data" and pkt.size:
                tel = klv.decode_tel(bytes(pkt)) or tel
                continue
            if pkt.stream.type != "video" or tel is None:
                continue
            for frame in pkt.decode():
                n += 1
                if n % args.step:
                    continue
                k = gt_frames[max(np.searchsorted(gt_frames, tel["frame"], "right") - 1, 0)]
                measure(pkg, frame.to_ndarray(format="bgr24"), tel, gt[k], err)

    print(f"{'class':18s} {'n':>5s} {'median px':>10s} {'p90 px':>8s} {'bias dx,dy':>12s} "
          f"{'box h px':>9s} {'err / box h':>12s}")
    for cls, rows in sorted(err.items()):
        a = np.array(rows)
        e = np.hypot(a[:, 0], a[:, 1])
        print(f"{cls:18s} {len(a):5d} {np.median(e):10.1f} {np.percentile(e, 90):8.1f} "
              f"{np.median(a[:, 0]):5.1f},{np.median(a[:, 1]):5.1f} {np.median(a[:, 2]):9.1f} "
              f"{np.median(e / a[:, 2]):12.2f}")


def measure(pkg, img, tel, objs, err) -> None:
    cam = Camera(tel)
    pos = np.array([o[1] for o in objs], float)
    is_soldier = np.array([bool(o[4]) for o in objs])  # vehicles have no stance
    boxes, _, cls = detect_batch(pkg, [img], pkg.conf_detect)[0]
    for box, c in zip(boxes, cls):
        name, grp = pkg.classes[c], pkg.group_of[c]
        if name in pkg.events:
            continue
        u, v, lift = anchor({"box": box, "grp": grp})
        sel = is_soldier if grp == "soldier" else ~is_soldier
        if not sel.any():
            continue
        px = cam.project(pos[sel] + [0, 0, lift])
        d = np.hypot(px[:, 0] - u, px[:, 1] - v)
        i = np.nanargmin(d) if np.isfinite(d).any() else None
        w, h = box[2] - box[0], box[3] - box[1]
        if i is None or d[i] > max(w, h):  # no true object near this box
            continue
        err[name].append((u - px[i, 0], v - px[i, 1], h))


if __name__ == "__main__":
    main()
