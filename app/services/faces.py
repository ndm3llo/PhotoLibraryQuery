"""Face detection and recognition embeddings (InsightFace `buffalo_l`).

How a photo is processed:
  1. Orient the image (EXIF), keep the FULL-RESOLUTION original.
  2. Detect faces on a downscaled copy (fast; the detector does not need more).
  3. Map each face's landmarks back to the original and compute its embedding from the original's pixels.
     A face that is 60 px wide in the 2048 px copy can be 170 px wide in a 12-megapixel original;
     step 3 is the one "zoom" that genuinely adds information (enlarging pixels afterwards does not).

Scores and sizes:
  - `det_score`: the detector's confidence that this is a face.
  - `px`: the shorter side of the face box, in ORIGINAL pixels.
  - `quality` = det_score * min(1, px / 112): 112 px is the recognition model's input size, so smaller faces
    are progressively less trustworthy. Used to decide what may be applied automatically vs only suggested.
  Faces below MIN_DET_SCORE or MIN_FACE_PX are dropped.

LICENSE: InsightFace's code is MIT, but its pretrained models are licensed for NON-COMMERCIAL research use only
(see docs/ARCHITECTURE.md, Phase 4). Fine for a private family album; revisit before any commercial use. The
model sits behind `FaceEngine` so it can be replaced; `MODEL_VERSION` is stored with every face.

Embeddings are L2-normalised, so cosine distance = 1 - dot product. Measured on the labeled sample set
(docs/ARCHITECTURE.md): the same person is typically within 0.30-0.42, strangers were never closer than 0.79.
"""
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image, ImageOps

MODEL_VERSION = "insightface-buffalo_l-v1"
MIN_DET_SCORE = 0.5
MIN_FACE_PX = 32
DETECT_LONG_EDGE = 2048  # detection runs on a copy no larger than this
EMBED_MAX_LONG_EDGE = 6000  # guard memory on very large panoramas/scans
EMBEDDING_DIM = 512


@dataclass(frozen=True)
class DetectedFace:
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2 in pixels of the ORIENTED ORIGINAL image
    det_score: float
    quality: float
    px: int  # shorter side of the box, original pixels
    embedding: list[float]  # 512 floats, L2-normalised


class FaceEngine(Protocol):
    model_version: str

    def detect(self, img: Image.Image) -> list[DetectedFace]: ...


def accept(det_score: float, px: float) -> bool:
    """Whether a detection is worth keeping at all."""
    return det_score >= MIN_DET_SCORE and px >= MIN_FACE_PX


def quality_score(det_score: float, px: float) -> float:
    return round(float(det_score) * min(1.0, float(px) / 112.0), 3)


class InsightFaceEngine:
    model_version = MODEL_VERSION

    def __init__(self, det_size: int = 640):
        from insightface.app import (
            FaceAnalysis,  # heavy import: only the worker / face search pays for it
        )

        self._app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"],
                                 allowed_modules=["detection", "recognition"])
        self._app.prepare(ctx_id=-1, det_size=(det_size, det_size))
        self._rec = self._app.models["recognition"]

    def detect(self, img: Image.Image) -> list[DetectedFace]:
        import cv2
        from insightface.app.common import Face

        oriented = ImageOps.exif_transpose(img).convert("RGB")
        long_edge = max(oriented.size)
        if long_edge > EMBED_MAX_LONG_EDGE:
            f = EMBED_MAX_LONG_EDGE / long_edge
            oriented = oriented.resize((round(oriented.width * f), round(oriented.height * f)), Image.Resampling.LANCZOS)
        full = np.ascontiguousarray(np.asarray(oriented)[:, :, ::-1])  # InsightFace / OpenCV expect BGR

        scale = min(1.0, DETECT_LONG_EDGE / max(full.shape[:2]))
        small = full if scale == 1.0 else cv2.resize(full, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        bboxes, kpss = self._app.det_model.detect(small, max_num=0, metric="default")
        if bboxes is None or bboxes.shape[0] == 0:
            return []

        faces = []
        H, W = full.shape[:2]
        for i in range(bboxes.shape[0]):
            x1, y1, x2, y2 = (bboxes[i, :4] / scale).tolist()
            score = float(bboxes[i, 4])
            px = min(x2 - x1, y2 - y1)
            if not accept(score, px):
                continue
            face = Face(bbox=bboxes[i, :4] / scale, kps=kpss[i] / scale, det_score=score)
            try:
                self._rec.get(full, face)  # aligns on the ORIGINAL pixels and embeds
            except Exception:
                continue  # degenerate landmarks; skip this face rather than fail the photo
            emb = np.asarray(face.embedding, dtype=np.float32)
            emb = emb / (np.linalg.norm(emb) + 1e-9)
            faces.append(DetectedFace(
                bbox=(max(0, round(x1)), max(0, round(y1)), min(W, round(x2)), min(H, round(y2))),
                det_score=round(score, 4), quality=quality_score(score, px), px=round(px),
                embedding=[float(v) for v in emb]))
        faces.sort(key=lambda f: -f.px)  # biggest face first
        return faces


_engine: FaceEngine | None = None


def get_engine() -> FaceEngine:
    """Process-wide engine, created on first use (loading the model takes a few seconds)."""
    global _engine
    if _engine is None:
        _engine = InsightFaceEngine()
    return _engine


def set_engine(engine: FaceEngine | None) -> None:
    """Override the engine (tests)."""
    global _engine
    _engine = engine


def detect_in_bytes(data: bytes) -> list[DetectedFace]:
    from app.services import imaging

    return get_engine().detect(imaging.open_image(data))
