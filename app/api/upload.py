import io
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from PIL import Image
from sqlmodel import Session, select

from app.auth.deps import current_user, get_rls_session, resolve_library
from app.auth.jwt import AuthUser
from app.db.rls import service_session
from app.models.model import Photo, PhotoFace
from app.services.ai import ai_service
from app.services.extract_metadata import get_photo_metadata
from app.services.gen_thumbnail import generate_thumbnail
from app.services.img_face_recognition import extract_face_embeddings
from app.services.storage import storage_service

router = APIRouter()


@router.post("/upload")
async def upload_photos(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    library_id: uuid.UUID | None = Form(None),
    user: AuthUser = Depends(current_user),
    session: Session = Depends(get_rls_session),
):
    """Upload photos into a library. The uploader is the signed-in user; owners and editors only."""
    lib = resolve_library(session, user.id, library_id, roles=("owner", "editor"))
    results = []

    for file in files:
        contents = await file.read()

        # 1. Duplicate check (pHash), scoped to this library
        current_phash = ai_service.generate_phash(contents)
        duplicate = session.exec(
            select(Photo).where(Photo.p_hash == current_phash, Photo.library_id == lib)
        ).first()
        if duplicate:
            results.append({"filename": file.filename, "status": "duplicate", "id": duplicate.id})
            continue

        # 2. Shell record, filled in by the background task
        new_photo = Photo(
            library_id=lib,
            uploader_id=user.id,
            status="processing",
            storage_path="pending",
            thumbnail_path="pending",
            p_hash=current_phash,
        )
        session.add(new_photo)
        session.commit()
        session.refresh(new_photo)

        # 3. Heavy work after the response
        background_tasks.add_task(process_and_upload, new_photo.id, contents, file.filename)
        results.append({"filename": file.filename, "status": "processing", "id": new_photo.id})

    return {"library_id": lib, "results": results}


def process_and_upload(photo_id: uuid.UUID, contents: bytes, filename: str):
    """Background task (trusted internal path: uses the service role, keyed by photo_id)."""
    try:
        metadata = get_photo_metadata(contents)
        vector = ai_service.generate_embedding_from_bytes(contents)
        face_vectors = extract_face_embeddings(contents)

        with Image.open(io.BytesIO(contents)) as img:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            original_io = io.BytesIO()
            img.save(original_io, format="JPEG", quality=95)
            original_bytes = original_io.getvalue()
        thumb_bytes = generate_thumbnail(contents)

        unique_filename = f"{photo_id}.jpg"
        storage_url = storage_service.upload_image(original_bytes, unique_filename, folder="originals")
        thumb_url = storage_service.upload_image(thumb_bytes, unique_filename, folder="thumbnails")

        with service_session() as session:
            db_photo = session.get(Photo, photo_id)
            if db_photo:
                db_photo.storage_path = storage_url
                db_photo.thumbnail_path = thumb_url
                db_photo.original_key = f"originals/{unique_filename}"
                db_photo.thumb_key = f"thumbnails/{unique_filename}"
                db_photo.clip_embedding = vector
                db_photo.location = metadata.get("location_wkt")
                db_photo.captured_at = metadata.get("date")
                db_photo.status = "ready"
                session.add(db_photo)

                for face_vector in face_vectors:
                    session.add(PhotoFace(
                        photo_id=photo_id,
                        library_id=db_photo.library_id,
                        face_embedding=face_vector,
                    ))
    except Exception as e:
        print(f"Background task failed for {photo_id}: {e}")
        try:
            with service_session() as session:
                db_photo = session.get(Photo, photo_id)
                if db_photo:
                    db_photo.status = "failed"
                    db_photo.error = str(e)[:500]
                    session.add(db_photo)
        except Exception as inner:
            print(f"Could not mark {photo_id} failed: {inner}")
