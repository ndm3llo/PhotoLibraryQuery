"""Face engine: pure rules always run; the real-model tests run when the InsightFace model is installed
(it is a ~280 MB one-time download, so CI skips them unless RUN_FACE_MODEL_TESTS=1)."""
import io
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from app.services import faces

MODEL_DIR = Path.home() / ".insightface" / "models" / "buffalo_l"
needs_model = pytest.mark.skipif(
    not (MODEL_DIR.exists() or os.getenv("RUN_FACE_MODEL_TESTS") == "1"),
    reason="InsightFace buffalo_l model not downloaded")


# ----------------------------------------------------------------------------- pure rules

@pytest.mark.parametrize("score,px,keep", [
    (0.9, 100, True), (0.5, 32, True), (0.49, 100, False), (0.99, 31, False), (0.3, 10, False), (0.95, 400, True)])
def test_which_detections_are_kept(score, px, keep):
    assert faces.accept(score, px) is keep


def test_quality_penalises_small_faces_but_caps_at_the_model_input_size():
    assert faces.quality_score(0.9, 112) == 0.9 and faces.quality_score(0.9, 500) == 0.9
    assert faces.quality_score(0.9, 56) == 0.45
    assert faces.quality_score(0.9, 56) < faces.quality_score(0.9, 112)
    assert faces.quality_score(0.5, 224) < faces.quality_score(0.9, 112)


def test_config_cutoffs_are_ordered_and_inside_the_measured_gap():
    from app.config import FACE_AUTO_MAX, FACE_AUTO_MIN_QUALITY, FACE_SUGGEST_MAX
    assert 0 < FACE_AUTO_MAX < FACE_SUGGEST_MAX < 0.79, "no stranger was ever closer than 0.79 in the evaluation"
    assert 0 < FACE_AUTO_MIN_QUALITY <= 1


# ----------------------------------------------------------------------- real model (opt-in)

@pytest.fixture(scope="module")
def engine():
    pytest.importorskip("insightface")
    return faces.InsightFaceEngine()


@pytest.fixture(scope="module")
def astronaut():
    """A public-domain NASA portrait that ships inside scikit-image."""
    skimage_data = pytest.importorskip("skimage.data")
    return Image.fromarray(skimage_data.astronaut())


def dist(a, b):
    return 1 - float(np.dot(a.embedding, b.embedding))


@needs_model
def test_finds_one_face_with_sane_geometry_and_a_unit_embedding(engine, astronaut):
    found = engine.detect(astronaut)
    assert len(found) == 1
    f = found[0]
    x1, y1, x2, y2 = f.bbox
    assert 0 <= x1 < x2 <= astronaut.width and 0 <= y1 < y2 <= astronaut.height
    assert f.px == min(x2 - x1, y2 - y1) or abs(f.px - min(x2 - x1, y2 - y1)) <= 2
    assert 0.5 <= f.det_score <= 1 and 0 < f.quality <= 1
    assert len(f.embedding) == faces.EMBEDDING_DIM and abs(np.linalg.norm(f.embedding) - 1) < 1e-4


@needs_model
def test_same_face_mirrored_or_rescaled_stays_close(engine, astronaut):
    base = engine.detect(astronaut)[0]
    mirrored = engine.detect(astronaut.transpose(Image.Transpose.FLIP_LEFT_RIGHT))[0]
    assert dist(base, mirrored) < 0.15
    big = astronaut.resize((3000, 3000), Image.Resampling.LANCZOS)  # larger than the 2048 detection copy
    found_big = engine.detect(big)
    assert len(found_big) == 1 and dist(base, found_big[0]) < 0.25
    assert found_big[0].px > base.px * 3, "the box is reported in ORIGINAL pixels, not the downscaled copy's"


@needs_model
def test_exif_orientation_is_respected(engine, astronaut):
    """Pixels stored rotated + an EXIF tag, as phones do, must give the same face in upright coordinates."""
    upright = engine.detect(astronaut)[0]
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 degrees clockwise to display
    buf = io.BytesIO()
    astronaut.rotate(90, expand=True).save(buf, "JPEG", exif=exif, quality=95)
    stored = Image.open(io.BytesIO(buf.getvalue()))
    found = engine.detect(stored)
    assert len(found) == 1 and dist(upright, found[0]) < 0.2
    ux1, uy1, ux2, uy2 = upright.bbox
    x1, y1, x2, y2 = found[0].bbox
    assert abs((x1 + x2) / 2 - (ux1 + ux2) / 2) < 25 and abs((y1 + y2) / 2 - (uy1 + uy2) / 2) < 25


@needs_model
def test_no_face_tiny_face_and_odd_color_modes(engine, astronaut):
    rng = np.random.default_rng(0)
    assert engine.detect(Image.fromarray(rng.integers(0, 255, (400, 400, 3), dtype=np.uint8))) == []
    assert engine.detect(astronaut.resize((60, 60))) == [], "a face smaller than MIN_FACE_PX is dropped"
    for mode in ("L", "RGBA", "P"):
        assert len(engine.detect(astronaut.convert(mode))) == 1, mode


@needs_model
def test_faces_come_back_biggest_first(engine, astronaut):
    canvas = Image.new("RGB", (1400, 700), "white")
    canvas.paste(astronaut.resize((300, 300)), (50, 50))
    canvas.paste(astronaut, (600, 100))
    found = engine.detect(canvas)
    assert len(found) == 2 and found[0].px > found[1].px
