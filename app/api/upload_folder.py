import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile
from sqlmodel import Session, select

from app.api.upload import process_and_upload
from app.auth.deps import current_user, get_rls_session, resolve_library
from app.auth.jwt import AuthUser
from app.models.model import Photo
from app.services.ai import ai_service

router = APIRouter()


@router.post("/upload-folder")
async def upload_folder(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    library_id: uuid.UUID | None = Form(None),
    user: AuthUser = Depends(current_user),
    session: Session = Depends(get_rls_session),
):
    """Experimental batch uploader (replaced by presigned uploads in Phase 3). Owners and editors only."""
    lib = resolve_library(session, user.id, library_id, roles=("owner", "editor"))
    results = []

    for file in files:
        contents = await file.read()

        current_phash = ai_service.generate_phash(contents)
        duplicate = session.exec(
            select(Photo).where(Photo.p_hash == current_phash, Photo.library_id == lib)
        ).first()
        if duplicate:
            results.append({"filename": file.filename, "status": "duplicate", "id": duplicate.id})
            continue

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

        background_tasks.add_task(process_and_upload, new_photo.id, contents, file.filename)
        results.append({"filename": file.filename, "status": "processing", "id": new_photo.id})

    return {
        "batch_id": str(uuid.uuid4()),
        "library_id": lib,
        "files_count": len(files),
        "results": results,
    }
