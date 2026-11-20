import uuid

from fastapi import APIRouter, BackgroundTasks, File, Form, UploadFile
from sqlmodel import Session, select

from app.api.upload import process_and_upload
from app.db.session import engine
from app.models.model import Photo
from app.services.ai import ai_service

router = APIRouter()

@router.post("/upload-folder")
async def upload_folder(
    background_tasks: BackgroundTasks, 
    files: list[UploadFile] = File(...),
    user_id: uuid.UUID = Form(...)
):
    """
    Experimental batch uploader. 
    Assumes all files in the upload are valid images for testing purposes.
    """
    results = []
    
    for file in files:
        # Read file into memory
        contents = await file.read()
        
        # 1. Deduplication Check (pHash)
        # Still recommended even for testing to avoid database clutter
        current_phash = ai_service.generate_phash(contents)
        with Session(engine) as session:
            duplicate = session.exec(
                select(Photo).where(Photo.p_hash == current_phash, Photo.user_id == user_id)
            ).first()
            
            if duplicate:
                results.append({"filename": file.filename, "status": "duplicate", "id": duplicate.id})
                continue

        # 2. Create the shell record
        new_photo = Photo(
            user_id=user_id,
            storage_path="pending",
            thumbnail_path="pending",
            p_hash=current_phash,
        )
        
        with Session(engine) as session:
            session.add(new_photo)
            session.commit()
            session.refresh(new_photo)

        # 3. Offload to the background worker (from upload.py)
        background_tasks.add_task(
            process_and_upload, 
            new_photo.id, 
            contents, 
            file.filename
        )

        results.append({
            "filename": file.filename, 
            "status": "processing", 
            "id": new_photo.id
        })

    return {
        "batch_id": str(uuid.uuid4()),
        "files_count": len(files),
        "results": results
    }