import uuid
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlmodel import Session, select

from app.db.session import engine
from app.models.model import Photo, PhotoFace
from app.services.img_face_recognition import extract_face_embeddings

router = APIRouter()

@router.post("/search-all-debug")
async def search_faces_debug(
    album_id: Optional[uuid.UUID] = Form(None), 
    file: UploadFile = File(...)
):
    """
    Returns lists of paths and full objects for 'chosen' and 'not_chosen' photos.
    The path lists are optimized for quick searching in Cloudflare/Storage.
    """
    contents = await file.read()
    face_vectors = extract_face_embeddings(contents)
    
    if not face_vectors:
        raise HTTPException(status_code=400, detail="No face detected.")
    
    query_vector = face_vectors[0]
    
    # Threshold 
    MATCH_THRESHOLD_DIST = 0.6

    with Session(engine) as session:
        distance_expr = PhotoFace.face_embedding.cosine_distance(query_vector)
        
        statement = (
            select(Photo, distance_expr.label("dist"))
            .join(PhotoFace, PhotoFace.photo_id == Photo.id)
        )

        if album_id:
            statement = statement.where(PhotoFace.album_id == album_id)
        
        statement = statement.order_by(distance_expr.asc())
        results = session.exec(statement).all()

        # New path-only lists for quick lookup
        chosen_paths = []
        not_chosen_paths = []
        
        # Original detailed lists
        chosen_objects = []
        not_chosen_objects = []
        
        seen_ids = set()

        for photo, dist in results:
            if photo.id in seen_ids:
                continue
            seen_ids.add(photo.id)
            
            dist_val = float(dist)
            confidence = round(1 - dist_val, 4)
            path = photo.storage_path
            
            photo_data = {
                "photo_id": photo.id,
                "storage_path": path,
                "confidence_score": confidence,
                "distance": round(dist_val, 4)
            }

            if dist_val < MATCH_THRESHOLD_DIST:
                chosen_paths.append(path)
                chosen_objects.append(photo_data)
            else:
                not_chosen_paths.append(path)
                not_chosen_objects.append(photo_data)

        return {
            "chosen_paths": chosen_paths,
            "not_chosen_paths": not_chosen_paths,
            "threshold": 1 - MATCH_THRESHOLD_DIST,
            "counts": {
                "chosen": len(chosen_objects),
                "not_chosen": len(not_chosen_objects)
            },
            "chosen": chosen_objects,
            "not_chosen": not_chosen_objects
        }