import io
import uuid

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from PIL import Image
from sqlmodel import Session, select

from app.db.session import engine  # Ensure this points to your new database.py
from app.models.model import Photo, PhotoFace  # Import PhotoFace
from app.services.ai import ai_service
from app.services.extract_metadata import get_photo_metadata
from app.services.gen_thumbnail import generate_thumbnail
from app.services.img_face_recognition import extract_face_embeddings
from app.services.storage import storage_service

router = APIRouter()

@router.post("/upload")
async def upload_photos(background_tasks: BackgroundTasks, files: list[UploadFile] = File(...), user_id: uuid.UUID = Form(...)):
    results = []
    
    for file in files:
        contents = await file.read()
        
        # 1. Immediate Check: pHash (Duplicates)
        # We do this before uploading to R2 to save bandwidth/money
        current_phash = ai_service.generate_phash(contents)
        with Session(engine) as session:
            duplicate = session.exec(select(Photo).where(Photo.p_hash == current_phash)).first()
            if duplicate:
                results.append({"filename": file.filename, "status": "duplicate"})
                continue

        # 2. Create "Pending" record in DB
        # This gives us a UUID and lets the user know the upload started
        new_photo = Photo(
            #user_id=uuid.uuid4(), # Replace with real user_id from Auth later
            user_id = user_id,
            storage_path="pending",
            thumbnail_path="pending",
            p_hash=current_phash,
            # we leave clip_embedding, metadata analysis (might change to account for metadata stripping), facial recognition, empty for the background task
        )
        
        with Session(engine) as session:
            session.add(new_photo)
            session.commit()
            session.refresh(new_photo)

        # 3. Offload the heavy work to the Background
        # This handles R2 upload + AI Vectorizing
        background_tasks.add_task(
            process_and_upload, 
            new_photo.id, 
            contents, 
            file.filename
        )

        results.append({"filename": file.filename, "status": "processing", "id": new_photo.id})

    return {"results": results}

# This function runs after the user gets their response
# Updated Background Task
def process_and_upload(photo_id: uuid.UUID, contents: bytes, filename: str):
    try:
        # A. AI Math
        metadata = get_photo_metadata(contents)
        vector = ai_service.generate_embedding_from_bytes(contents)
        
        face_vectors = extract_face_embeddings(contents)

        with Image.open(io.BytesIO(contents)) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            original_io = io.BytesIO()
            # We use 95 quality so it stays "high res" but is a valid JPEG
            img.save(original_io, format="JPEG", quality=95)
            original_bytes = original_io.getvalue()
        thumb_bytes = generate_thumbnail(contents)
        
        # C. Upload to R2 (Two calls, different folders)
        # Use the photo_id as the name to ensure it's truly unique
        unique_filename = f"{photo_id}.jpg" 
        
        storage_url = storage_service.upload_image(original_bytes, unique_filename, folder="originals")
        thumb_url = storage_service.upload_image(thumb_bytes, unique_filename, folder="thumbnails")
        
        # D. Final Database Update
        with Session(engine) as session:
            db_photo = session.get(Photo, photo_id)
            if db_photo:
                db_photo.storage_path = storage_url
                db_photo.thumbnail_path = thumb_url # Success!
                db_photo.clip_embedding = vector
                db_photo.location = metadata.get("location_wkt")
        
                db_photo.captured_at = metadata.get("date")
                session.add(db_photo)

                for vector in face_vectors:
                    new_face = PhotoFace(
                        photo_id=photo_id,
                        album_id=db_photo.album_id, # Scoped to album for security
                        face_embedding=vector
                    )
                    session.add(new_face)
                session.commit()
            
    except Exception as e:
        print(f"Background task failed for {photo_id}: {e}")