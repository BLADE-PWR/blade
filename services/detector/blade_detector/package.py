"""Model package: models/<name>/{model.onnx, model.yaml, tracker.yaml}, with its own preprocessing."""
from __future__ import annotations

import logging
import types
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import yaml

log = logging.getLogger("detector.package")


@dataclass
class ModelPackage:
    root: Path
    name: str
    version: int
    imgsz: int
    classes: list[str]
    group_of: list[str]
    events: set[str]
    conf_detect: float
    conf_track: float
    nms_iou: float
    enhance: bool
    tracker_args: types.SimpleNamespace
    max_size_m: dict = field(default_factory=dict)  # group -> longest box side on the ground [m]
    auto_imgsz: dict = field(default_factory=dict)  # {max, object_m, min_px}: input size from the range
    scale: dict = field(default_factory=dict)  # {object_m, min_px, max_px}: scales the auto model choice may use it at
    meta: dict = field(repr=False, default_factory=dict)
    _session: object = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def load(cls, root: str | Path) -> ModelPackage:
        root = Path(root)
        m = yaml.safe_load((root / "model.yaml").read_text())
        classes = list(m["classes"])
        group_of = list(classes)
        for grp, idx in (m.get("groups") or {}).items():
            for i in idx:
                group_of[i] = grp
        tr = yaml.safe_load((root / "tracker.yaml").read_text()) if (root / "tracker.yaml").exists() else {}
        conf = m.get("conf") or {}
        return cls(root=root, name=m["name"], version=int(m.get("version", 1)), imgsz=int(m.get("imgsz", 1280)),
                   classes=classes, group_of=group_of, events=set(m.get("events") or []),
                   conf_detect=float(conf.get("detect", 0.25)), conf_track=float(conf.get("track", 0.1)),
                   nms_iou=float(m.get("nms_iou", 0.7)), enhance=bool((m.get("preprocess") or {}).get("enhance")),
                   tracker_args=types.SimpleNamespace(**tr), max_size_m=dict(m.get("max_size_m") or {}),
                   auto_imgsz=dict(m.get("auto_imgsz") or {}), scale=dict(m.get("scale") or {}), meta=m)

    @property
    def imgsz_max(self) -> int:
        """The largest model input auto_imgsz may pick."""
        return int(self.auto_imgsz.get("max", self.imgsz))

    @property
    def max_m(self) -> np.ndarray:
        """Per class: longest box side on the ground its group can fill, inf without a limit."""
        return np.array([float(self.max_size_m.get(g, np.inf)) for g in self.group_of])

    def session(self):
        if self._session is None:
            import onnxruntime as ort
            avail = ort.get_available_providers()
            if "CUDAExecutionProvider" in avail and hasattr(ort, "preload_dlls"):
                try:
                    ort.preload_dlls()  # CUDA / cuDNN from the nvidia-* wheels
                except Exception as e:
                    log.warning("preload_dlls: %s", e)
            prov = [p for p in ("CUDAExecutionProvider", "CoreMLExecutionProvider", "CPUExecutionProvider")
                    if p in avail and p != "CoreMLExecutionProvider"]  # CoreML: dynamic shapes are slow
            self._session = ort.InferenceSession(str(self.root / "model.onnx"), providers=prov)
            log.info("%s on %s", self.id, self._session.get_providers()[0])
        return self._session

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version, "imgsz": self.imgsz,
                "classes": self.classes, "events": sorted(self.events), "conf": {"detect": self.conf_detect,
                "track": self.conf_track}, "enhance": self.enhance, "max_size_m": self.max_size_m,
                "auto_imgsz": self.auto_imgsz, "scale": self.scale, "license": self.meta.get("license"),
                "loaded": self._session is not None}


def find_packages(models_dir: Path) -> dict[str, ModelPackage]:
    out = {}
    for d in sorted(models_dir.iterdir() if models_dir.exists() else []):
        if (d / "model.yaml").exists() and (d / "model.onnx").exists():
            try:
                p = ModelPackage.load(d)
                out[p.id] = p
            except Exception:
                log.exception("%s: bad model package", d)
    return out


def letterbox(img: np.ndarray, size: int, stride: int = 32):
    """Aspect-preserving resize + grey padding to a multiple of stride, exactly as Ultralytics does."""
    import cv2

    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = round(w * r), round(h * r)
    dw, dh = ((size - nw) % stride) / 2, ((size - nh) % stride) / 2
    if (nw, nh) != (w, h):
        img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    out = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=(114, 114, 114))
    return out, r, left, top


def nms(b: np.ndarray, s: np.ndarray, thr: float) -> list[int]:
    area = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    order, keep = s.argsort()[::-1], []
    while order.size:
        i, rest = order[0], order[1:]
        keep.append(int(i))
        xx1 = np.maximum(b[i, 0], b[rest, 0])
        yy1 = np.maximum(b[i, 1], b[rest, 1])
        xx2 = np.minimum(b[i, 2], b[rest, 2])
        yy2 = np.minimum(b[i, 3], b[rest, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        iou = inter / (area[i] + area[rest] - inter + 1e-9)
        order = rest[iou <= thr]
    return keep


def detect_batch(pkg: ModelPackage, images_bgr: list[np.ndarray], conf: float, max_det: int = 300,
                 imgsz: int | None = None):
    """BGR images of one size, at model input imgsz (default the package's) -> per image (xyxy (n,4) in image
    pixels, conf (n,), cls (n,))."""
    if not images_bgr:
        return []
    size = imgsz or pkg.imgsz
    boxes = []
    xs, geo = [], []
    for img in images_bgr:
        if pkg.enhance:
            from PIL import Image

            from blade_enhance import enhance
            img = np.asarray(enhance(Image.fromarray(img[:, :, ::-1])))[:, :, ::-1]
        x, r, left, top = letterbox(img, size)
        xs.append(x[:, :, ::-1].transpose(2, 0, 1))
        geo.append((r, left, top, img.shape[1], img.shape[0]))
    batch = np.ascontiguousarray(np.stack(xs), dtype=np.float32) / 255.0
    sess = pkg.session()
    out = sess.run(None, {sess.get_inputs()[0].name: batch})[0]  # (B, 4 + nc, anchors)
    for k, (r, left, top, w, h) in enumerate(geo):
        p = out[k].T
        scores = p[:, 4:]
        cl, cf = scores.argmax(1), scores.max(1)
        m = cf >= conf
        p, cl, cf = p[m], cl[m], cf[m]
        if not len(p):
            boxes.append((np.zeros((0, 4), np.float32), np.zeros(0, np.float32), np.zeros(0, np.int64)))
            continue
        b = np.stack([p[:, 0] - p[:, 2] / 2, p[:, 1] - p[:, 3] / 2, p[:, 0] + p[:, 2] / 2, p[:, 1] + p[:, 3] / 2], 1)
        keep = nms(b + cl[:, None] * 4 * size, cf, pkg.nms_iou)[:max_det]
        b, cl, cf = b[keep], cl[keep], cf[keep]
        b[:, [0, 2]] = np.clip((b[:, [0, 2]] - left) / r, 0, w)
        b[:, [1, 3]] = np.clip((b[:, [1, 3]] - top) / r, 0, h)
        boxes.append((b.astype(np.float32), cf.astype(np.float32), cl.astype(np.int64)))
    return boxes
