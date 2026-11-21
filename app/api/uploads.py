"""Direct-to-storage uploads.

  1. POST /api/uploads            -> a photo row per file (status 'pending') and a short-lived presigned PUT URL
  2. the browser PUTs each file straight to the bucket (the API never touches the bytes)
  3. POST /api/uploads/complete   -> we verify the object exists and matches, then queue processing

Only `Content-Type` is signed, so the browser needs to send nothing else (see infra/r2-cors.json).
Owners and editors only; the uploader is always the signed-in user.
"""
import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel import Session

from app.auth.deps import current_user, get_rls_session, resolve_library
from app.auth.jwt import AuthUser
from app.db.rls import service_session
from app.services import jobs
from app.services.pipeline import MAX_UPLOAD_BYTES
from app.services.storage import PUT_URL_TTL_SECONDS, get_storage, original_key

router = APIRouter()

ALLOWED_TYPES = {"image/jpeg", "image/png", "image/heic", "image/heif", "image/webp"}
EXTENSION_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                   ".heic": "image/heic", ".heif": "image/heif", ".webp": "image/webp"}
MAX_FILES_PER_REQUEST = 100
MAX_COMPLETE_IDS = 200


class FileSpec(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=1)
    content_type: str = Field(default="", max_length=100)
    last_modified: int | None = Field(default=None, description="File.lastModified, ms since epoch")


class UploadRequest(BaseModel):
    library_id: uuid.UUID | None = None
    files: list[FileSpec] = Field(min_length=1, max_length=MAX_FILES_PER_REQUEST)


class CompleteRequest(BaseModel):
    photo_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_COMPLETE_IDS)


def _clean_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/"))
    return re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:255] or "upload"


def _content_type(spec: FileSpec) -> str | None:
    """Trust the extension when the browser gives no (or an unknown) type; HEIC often arrives untyped."""
    declared = spec.content_type.lower().split(";")[0].strip()
    if declared in ALLOWED_TYPES:
        return declared
    return EXTENSION_TYPES.get(os.path.splitext(spec.filename.lower())[1])


def _mtime(ms: int | None) -> datetime | None:
    if ms is None or ms <= 0:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


@router.post("")
def start_uploads(body: UploadRequest, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    lib = resolve_library(session, user.id, body.library_id, roles=("owner", "editor"))
    storage = get_storage()
    batch_id = uuid.uuid4()
    results, rows = [], []

    for spec in body.files:
        filename = _clean_filename(spec.filename)
        content_type = _content_type(spec)
        if content_type is None:
            results.append({"filename": filename, "error": "Unsupported file type (use JPEG, PNG, HEIC or WebP)"})
            continue
        if spec.size > MAX_UPLOAD_BYTES:
            results.append({"filename": filename, "error": f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"})
            continue
        photo_id = uuid.uuid4()
        key = original_key(lib, photo_id)
        rows.append({"id": photo_id, "l": lib, "u": user.id, "k": key, "f": filename, "ct": content_type,
                     "s": spec.size, "m": _mtime(spec.last_modified), "b": batch_id})
        results.append({
            "photo_id": photo_id,
            "filename": filename,
            "upload_url": storage.presign_put(key, content_type),
            "method": "PUT",
            "headers": {"Content-Type": content_type},
            "expires_in": PUT_URL_TTL_SECONDS,
        })

    for row in rows:
        session.execute(
            text("INSERT INTO photo (id, library_id, uploader_id, status, original_key, original_filename, "
                 "content_type, byte_size, client_last_modified, batch_id) "
                 "VALUES (:id, :l, :u, 'pending', :k, :f, :ct, :s, :m, :b)"), row)
    session.commit()
    return {"batch_id": batch_id, "library_id": lib, "uploads": results}


@router.post("/complete")
def complete_uploads(body: CompleteRequest, user: AuthUser = Depends(current_user),
                     session: Session = Depends(get_rls_session)):
    """Call after the browser finished its PUTs. Idempotent: repeating it just reports current statuses."""
    storage = get_storage()
    ids = list(dict.fromkeys(body.photo_ids))
    found = {
        r.id: r for r in session.execute(
            text("SELECT id, status, original_key, byte_size FROM photo "
                 "WHERE id = ANY(:ids) AND uploader_id = :u"), {"ids": ids, "u": user.id}).all()
    }

    results = []
    for pid in ids:
        row = found.get(pid)
        if row is None:
            results.append({"photo_id": pid, "status": "not_found"})
            continue
        if row.status != "pending":
            results.append({"photo_id": pid, "status": row.status})  # already queued/processed
            continue
        head = storage.head(row.original_key)
        if head is None:
            results.append({"photo_id": pid, "status": "not_uploaded"})
        elif head["size"] > MAX_UPLOAD_BYTES:
            storage.delete(row.original_key)
            results.append({"photo_id": pid, "status": "too_large"})
        elif row.byte_size and head["size"] != row.byte_size:
            results.append({"photo_id": pid, "status": "size_mismatch"})
        else:
            # Status change and queue insert commit together, so a photo is never 'processing' with no job.
            with service_session() as s:
                changed = s.execute(
                    text("UPDATE photo SET status = 'processing', byte_size = :s WHERE id = :id AND status = 'pending'"),
                    {"s": head["size"], "id": pid}).rowcount
                if changed:
                    jobs.enqueue(s, "process_photo", {"photo_id": str(pid)}, dedupe_key=f"process_photo:{pid}")
            results.append({"photo_id": pid, "status": "processing"})
    return {"results": results}
