import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.auth.deps import get_rls_session
from app.models.model import Photo, PhotoFace
from app.services.img_face_recognition import extract_face_embeddings

router = APIRouter()

MATCH_THRESHOLD_DIST = 0.6  # too loose for production; retuned from an eval in Phase 4/5


@router.post("/search-all-debug")
async def search_faces_debug(
    library_id: Optional[uuid.UUID] = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_rls_session),
):
    """Debug: every photo in your libraries ranked by face distance, split at the threshold."""
    contents = await file.read()
    face_vectors = extract_face_embeddings(contents)
    if not face_vectors:
        raise HTTPException(status_code=400, detail="No face detected.")

    query_vector = face_vectors[0]
    distance_expr = PhotoFace.face_embedding.cosine_distance(query_vector)

    statement = select(Photo, distance_expr.label("dist")).join(PhotoFace, PhotoFace.photo_id == Photo.id)
    if library_id:
        statement = statement.where(PhotoFace.library_id == library_id)
    results = session.exec(statement.order_by(distance_expr.asc())).all()

    chosen, not_chosen = [], []
    seen_ids = set()
    for photo, dist in results:
        if photo.id in seen_ids:
            continue
        seen_ids.add(photo.id)
        dist_val = float(dist)
        item = {
            "photo_id": photo.id,
            "key": photo.original_key,
            "confidence_score": round(1 - dist_val, 4),
            "distance": round(dist_val, 4),
        }
        (chosen if dist_val < MATCH_THRESHOLD_DIST else not_chosen).append(item)

    return {
        "chosen_keys": [c["key"] for c in chosen],
        "not_chosen_keys": [c["key"] for c in not_chosen],
        "threshold": 1 - MATCH_THRESHOLD_DIST,
        "counts": {"chosen": len(chosen), "not_chosen": len(not_chosen)},
        "chosen": chosen,
        "not_chosen": not_chosen,
    }
