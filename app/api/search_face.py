import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.db.session import engine
from app.models.model import Photo, PhotoFace
from app.services.img_face_recognition import extract_face_embeddings

router = APIRouter()

@router.post("/face")
async def search_by_face(
    album_id: Optional[uuid.UUID] = Form(None), 
    file: UploadFile = File(...)
):
    contents = await file.read()
    
    face_vectors = extract_face_embeddings(contents)
    
    if not face_vectors:
        raise HTTPException(
            status_code=400, 
            detail="AI could not detect a clear face in the uploaded image."
        )
    
    query_vector = face_vectors[0]

    with Session(engine) as session:
        # 1. Define the distance calculation
        # We define it here so we can reuse it in SELECT, WHERE, and ORDER BY
        distance_expr = PhotoFace.face_embedding.cosine_distance(query_vector)
        
        # 2. Build the Query
        # We repeat the distance expression in .where() to satisfy Postgres rules
        statement = (
            select(Photo, distance_expr.label("dist"))
            .join(PhotoFace, PhotoFace.photo_id == Photo.id)
        )

        if album_id:
            statement = statement.where(PhotoFace.album_id == album_id)
        
        # FIX: We use the expression itself in the where clause instead of the string 'dist'
        statement = (
            statement.where(distance_expr < 0.6) 
            .order_by(distance_expr.asc())
            .limit(50)
        )
        
        results = session.exec(statement).all()

        unique_matches = {}
        for photo, dist in results:
            if photo.id not in unique_matches:
                unique_matches[photo.id] = {
                    "photo_id": photo.id,
                    "storage_path": photo.storage_path,
                    "thumbnail_path": photo.thumbnail_path,
                    "confidence_score": round(1 - float(dist), 4),
                    "captured_at": photo.captured_at
                }

        return {
            "faces_detected_in_search": len(face_vectors),
            "match_count": len(unique_matches),
            "results": list(unique_matches.values())
        }