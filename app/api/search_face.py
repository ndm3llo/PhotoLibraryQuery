import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.auth.deps import get_rls_session
from app.models.model import Photo, PhotoFace
from app.services.img_face_recognition import extract_face_embeddings
from app.services.storage import get_storage

router = APIRouter()


@router.post("/face")
async def search_by_face(
    library_id: Optional[uuid.UUID] = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_rls_session),
):
    """Find photos containing a face like the uploaded one, within your libraries (Phase 5 replaces this)."""
    contents = await file.read()
    face_vectors = extract_face_embeddings(contents)

    if not face_vectors:
        raise HTTPException(
            status_code=400,
            detail="AI could not detect a clear face in the uploaded image.",
        )

    query_vector = face_vectors[0]
    distance_expr = PhotoFace.face_embedding.cosine_distance(query_vector)

    # Row Level Security limits both tables to the caller's libraries.
    statement = select(Photo, distance_expr.label("dist")).join(PhotoFace, PhotoFace.photo_id == Photo.id)
    if library_id:
        statement = statement.where(PhotoFace.library_id == library_id)

    statement = statement.where(distance_expr < 0.6).order_by(distance_expr.asc()).limit(50)
    results = session.exec(statement).all()

    storage = get_storage()
    unique_matches = {}
    for photo, dist in results:
        if photo.id not in unique_matches:
            unique_matches[photo.id] = {
                "photo_id": photo.id,
                "library_id": photo.library_id,
                "display_url": storage.presign_get(photo.display_key) if photo.display_key else None,
                "thumbnail_url": storage.presign_get(photo.thumb_key) if photo.thumb_key else None,
                "confidence_score": round(1 - float(dist), 4),
                "taken_at": photo.taken_at,
            }

    return {
        "faces_detected_in_search": len(face_vectors),
        "match_count": len(unique_matches),
        "results": list(unique_matches.values()),
    }
