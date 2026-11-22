import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.auth.deps import get_rls_session
from app.config import FACE_AUTO_MAX, FACE_SUGGEST_MAX
from app.models.model import Photo, PhotoFace
from app.services import faces

router = APIRouter()


@router.post("/search-all-debug")
async def search_faces_debug(
    library_id: Optional[uuid.UUID] = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_rls_session),
):
    """Debug: every photo in your libraries ranked by face distance, split into 'auto', 'suggest' and 'no match'
    at the configured cutoffs."""
    contents = await file.read()
    try:
        found = faces.detect_in_bytes(contents)
    except Exception:
        found = []
    if not found:
        raise HTTPException(status_code=400, detail="No clear face was found in the uploaded image.")

    distance = PhotoFace.embedding.cosine_distance(found[0].embedding)
    statement = (select(Photo, distance.label("dist"), PhotoFace.quality)
                 .join(PhotoFace, PhotoFace.photo_id == Photo.id)
                 .where(PhotoFace.model_version == faces.MODEL_VERSION))
    if library_id:
        statement = statement.where(PhotoFace.library_id == library_id)

    tiers = {"auto": [], "suggest": [], "no_match": []}
    seen = set()
    for photo, dist, quality in session.exec(statement.order_by(distance.asc())).all():
        if photo.id in seen:
            continue
        seen.add(photo.id)
        d = float(dist)
        tier = "auto" if d <= FACE_AUTO_MAX else "suggest" if d <= FACE_SUGGEST_MAX else "no_match"
        tiers[tier].append({"photo_id": photo.id, "key": photo.original_key, "distance": round(d, 4),
                            "face_quality": quality})

    return {"cutoffs": {"auto_max": FACE_AUTO_MAX, "suggest_max": FACE_SUGGEST_MAX},
            "counts": {k: len(v) for k, v in tiers.items()}, **tiers}
