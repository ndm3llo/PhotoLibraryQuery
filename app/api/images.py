import io
import os
import zipfile
from typing import List

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy import text
from sqlmodel import Session

from app.auth.deps import get_rls_session
from app.services.storage import storage_service

router = APIRouter()


def _normalize_key(file_key: str) -> str:
    """Accept a bare object key or a full URL from the DB; reject anything odd."""
    public_base = os.getenv("R2_PUBLIC_URL") or ""
    key = file_key.replace(f"{public_base}/", "") if public_base else file_key
    key = key.lstrip("/")
    if not key or ".." in key.split("/"):
        raise HTTPException(status_code=400, detail="Invalid file key")
    return key


def _authorized_keys(session: Session, keys: list[str]) -> set[str]:
    """Keys that belong to a photo in a library the caller is a member of (enforced by RLS)."""
    rows = session.execute(
        text("SELECT original_key, display_key, thumb_key FROM photo "
             "WHERE original_key = ANY(:k) OR display_key = ANY(:k) OR thumb_key = ANY(:k)"),
        {"k": keys},
    ).all()
    allowed = {k for row in rows for k in row if k}
    return allowed & set(keys)


# --- Single view ---
@router.get("/view/{file_key:path}")
async def view_photo(file_key: str, session: Session = Depends(get_rls_session)):
    key = _normalize_key(file_key)
    # 404 (not 403) for files the caller may not see, so existence is not revealed.
    if key not in _authorized_keys(session, [key]):
        raise HTTPException(status_code=404, detail="Not found")
    try:
        image_bytes = storage_service.get_image_bytes(key)
    except Exception:
        raise HTTPException(status_code=404, detail="Not found") from None

    lower = key.lower()
    if lower.endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    elif lower.endswith(".png"):
        mime = "image/png"
    elif lower.endswith(".webp"):
        mime = "image/webp"
    elif lower.endswith(".heic"):
        mime = "image/heic"
    else:
        mime = "application/octet-stream"

    return Response(
        content=image_bytes,
        media_type=mime,
        headers={"Content-Disposition": "inline", "Cache-Control": "private, max-age=300"},
    )


# --- Batch download as ZIP ---
@router.post("/batch-download")
async def download_batch_photos(
    file_keys: List[str] = Body(..., examples=[["originals/uuid1.jpg", "originals/uuid2.jpg"]]),
    session: Session = Depends(get_rls_session),
):
    """ZIP of the requested files. Fails with 404 if any file is not one the caller may see."""
    if not file_keys:
        raise HTTPException(status_code=400, detail="List of paths is empty")
    if len(file_keys) > 500:
        raise HTTPException(status_code=400, detail="Too many files requested (max 500)")

    keys = [_normalize_key(k) for k in file_keys]
    if set(keys) - _authorized_keys(session, keys):
        raise HTTPException(status_code=404, detail="One or more files were not found")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for key in keys:
            try:
                zip_file.writestr(key.split("/")[-1], storage_service.get_image_bytes(key))
            except Exception as e:
                print(f"Skipping {key} due to error: {e}")
                continue

    zip_buffer.seek(0)
    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=photos.zip"},
    )
