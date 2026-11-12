"""
faces.py
--------
Face detection for Ward's "face-aware" protection pass.

WHAT THIS IS
    Classical Viola-Jones Haar-cascade detection (frontal + profile),
    using the cascades that ship inside OpenCV -- no model download, no
    network call, no GPU. It finds face *bounding boxes* only. It is not a face
    recognition/embedding model and has nothing to do with Identity
    Mode (Fawkes-style recognition-blinding) -- see core/cloak.py and
    README "What Ward does not do" for that distinction.

WHY IT EXISTS
    A cropped-out face is one of the most common sub-images to get
    reverse-image-searched on its own (profile pictures, face-swap
    tools, dating-profile scrapers all crop to the face first). Ward's
    main cloak (cloak.py) disrupts the hash of the *whole* image; a
    tight crop of just the face computes its own, different pHash that
    the whole-image cloak never touched. This module finds where those
    crops would land so cloak.py can give that region its own, second
    localized disruption pass.

ACCURACY NOTE
    Haar cascades are ~20 years old and miss faces at odd angles, poor
    lighting, or small sizes. This is a best-effort pass, not a
    guarantee every face is caught -- Ward reports exactly how many
    faces it found so the UI never overstates it as "verified."
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

# OpenCV ships these cascades inside its own package, so Ward doesn't
# bundle copies (they were byte-identical and made up most of the zip).
_FRONTAL_PATH = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
_PROFILE_PATH = os.path.join(cv2.data.haarcascades, "haarcascade_profileface.xml")

# Detection runs on a copy downscaled to at most this on the long side --
# see detect_faces' PERFORMANCE NOTE for why this doesn't cost recall.
_DETECT_MAX_DIM = 1024

_frontal_cascade: cv2.CascadeClassifier | None = None
_profile_cascade: cv2.CascadeClassifier | None = None


def _get_cascades() -> tuple[cv2.CascadeClassifier, cv2.CascadeClassifier]:
    """Lazy-load: keeps import-time cost at zero for requests that never
    touch face detection, and means a single process only pays the (small,
    ~1ms) XML parse cost once."""
    global _frontal_cascade, _profile_cascade
    if _frontal_cascade is None:
        for path in (_FRONTAL_PATH, _PROFILE_PATH):
            if not os.path.exists(path):
                raise FileNotFoundError(f"Missing OpenCV cascade at {path} -- reinstall opencv-python-headless.")
        _frontal_cascade = cv2.CascadeClassifier(_FRONTAL_PATH)
        _profile_cascade = cv2.CascadeClassifier(_PROFILE_PATH)
    return _frontal_cascade, _profile_cascade


@dataclass
class FaceBox:
    x: int
    y: int
    w: int
    h: int

    def padded(self, image_w: int, image_h: int, pad_frac: float = 0.35) -> "FaceBox":
        """Grow the box by pad_frac on each side (clamped to image bounds).
        Padding matters here specifically because a scraper cropping a
        face rarely crops tight to the Haar box -- profile pictures and
        face-crop tools almost always grab some hair/shoulders margin
        too, so the localized cloak pass needs to cover that margin to
        still land on the crop the scraper actually hashes."""
        pad_x = int(self.w * pad_frac)
        pad_y = int(self.h * pad_frac)
        x0 = max(0, self.x - pad_x)
        y0 = max(0, self.y - pad_y)
        x1 = min(image_w, self.x + self.w + pad_x)
        y1 = min(image_h, self.y + self.h + pad_y)
        return FaceBox(x0, y0, x1 - x0, y1 - y0)

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)


def detect_faces(image: Image.Image, min_size_frac: float = 0.04) -> list[FaceBox]:
    """Detect faces in a PIL image. Runs both frontal and profile
    cascades (the profile one is also run mirrored, to catch faces
    looking the other way) and merges overlapping detections.

    min_size_frac: skip detections smaller than this fraction of the
    image's shorter side -- tiny false-positive "faces" (a knuckle, a
    doorknob) are a known Haar-cascade failure mode, and a face too
    small to meaningfully crop-and-reverse-search isn't worth spending
    the cloak's visible-change budget on.

    PERFORMANCE NOTE: Haar-cascade cost scales with pixel count, but
    detection accuracy doesn't keep improving past a fairly modest
    resolution -- min_size_frac already expresses "how big a face must
    be, relative to the frame" scale-independently, so detecting on a
    smaller copy and mapping boxes back finds the same faces for a
    fraction of the cost on a large upload (Ward allows up to 8000px --
    see pipeline.MAX_DIMENSION). Verified against a real photo that the same face is found, in
    equivalent full-image coordinates, with and without this downscale.
    """
    frontal, profile = _get_cascades()
    rgb = image.convert("RGB")
    w, h = rgb.size

    long_side = max(w, h)
    scale = min(1.0, _DETECT_MAX_DIM / long_side) if long_side > 0 else 1.0
    if scale < 1.0:
        det_w, det_h = max(1, round(w * scale)), max(1, round(h * scale))
        det_rgb = rgb.resize((det_w, det_h), Image.BILINEAR)  # detection-only; speed over quality
    else:
        det_rgb, det_w, det_h = rgb, w, h

    gray = cv2.cvtColor(np.asarray(det_rgb), cv2.COLOR_RGB2GRAY)
    gray = cv2.equalizeHist(gray)

    min_dim = int(min(det_w, det_h) * min_size_frac)
    min_dim = max(min_dim, 20)

    def _run(cascade: cv2.CascadeClassifier, img: np.ndarray) -> list[tuple[int, int, int, int]]:
        return list(
            cascade.detectMultiScale(
                img, scaleFactor=1.1, minNeighbors=5, minSize=(min_dim, min_dim)
            )
        )

    raw: list[tuple[int, int, int, int]] = []
    raw += _run(frontal, gray)
    raw += _run(profile, gray)
    mirrored = cv2.flip(gray, 1)
    for (x, y, bw, bh) in _run(profile, mirrored):
        raw.append((det_w - x - bw, y, bw, bh))  # map back to (downscaled) original orientation

    inv = 1.0 / scale  # map detection-space boxes back to the real, full-resolution image
    boxes = [
        FaceBox(int(x * inv), int(y * inv), int(bw * inv), int(bh * inv))
        for (x, y, bw, bh) in raw
    ]
    return _merge_overlapping(boxes)


def _iou(a: FaceBox, b: FaceBox) -> float:
    ax1, ay1, ax2, ay2 = a.x, a.y, a.x + a.w, a.y + a.h
    bx1, by1, bx2, by2 = b.x, b.y, b.x + b.w, b.y + b.h
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a.w * a.h + b.w * b.h - inter
    return inter / union if union > 0 else 0.0


def _merge_overlapping(boxes: list[FaceBox], iou_thresh: float = 0.3) -> list[FaceBox]:
    """Frontal + profile + mirrored-profile commonly all fire on the same
    face -- collapse anything that overlaps into one box (the union of
    the overlapping boxes, so the merged box still covers what each
    detector individually flagged)."""
    merged: list[FaceBox] = []
    used = [False] * len(boxes)
    for i, box in enumerate(boxes):
        if used[i]:
            continue
        group = [box]
        used[i] = True
        for j in range(i + 1, len(boxes)):
            if not used[j] and _iou(box, boxes[j]) > iou_thresh:
                group.append(boxes[j])
                used[j] = True
        x0 = min(b.x for b in group)
        y0 = min(b.y for b in group)
        x1 = max(b.x + b.w for b in group)
        y1 = max(b.y + b.h for b in group)
        merged.append(FaceBox(x0, y0, x1 - x0, y1 - y0))
    return merged
