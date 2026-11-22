import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.auth.deps import get_rls_session
from app.config import FACE_SUGGEST_MAX
from app.models.model import Photo, PhotoFace
from app.services import faces
from app.services.storage import get_storage

router = APIRouter()


@router.post("/face")
async def search_by_face(
    library_id: Optional[uuid.UUID] = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_rls_session),
):
    """Find photos containing a face like the largest one in the uploaded image, within your libraries.
    (Prototype; Phase 5 replaces it with search by named person.)"""
    contents = await file.read()
    try:
        found = faces.detect_in_bytes(contents)
    except Exception:
        found = []
    if not found:
        raise HTTPException(status_code=400, detail="No clear face was found in the uploaded image.")

    query = found[0].embedding  # biggest face first
    distance = PhotoFace.embedding.cosine_distance(query)

    # Row Level Security limits both tables to the caller's libraries. Only faces from the CURRENT model are
    # comparable: embeddings from different models live in different spaces.
    statement = (select(Photo, distance.label("dist")).join(PhotoFace, PhotoFace.photo_id == Photo.id)
                 .where(PhotoFace.model_version == faces.MODEL_VERSION))
    if library_id:
        statement = statement.where(PhotoFace.library_id == library_id)
    statement = statement.where(distance < FACE_SUGGEST_MAX).order_by(distance.asc()).limit(50)

    storage = get_storage()
    unique_matches = {}
    for photo, dist in session.exec(statement).all():
        if photo.id not in unique_matches:
            unique_matches[photo.id] = {
                "photo_id": photo.id,
                "library_id": photo.library_id,
                "display_url": storage.presign_get(photo.display_key) if photo.display_key else None,
                "thumbnail_url": storage.presign_get(photo.thumb_key) if photo.thumb_key else None,
                "distance": round(float(dist), 4),
                "taken_at": photo.taken_at,
            }

    return {
        "faces_detected_in_search": len(found),
        "match_count": len(unique_matches),
        "results": list(unique_matches.values()),
    }
