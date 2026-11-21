"""Browse, view, correct and delete photos. All reads and writes go through Row Level Security."""
import base64
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session

from app.auth.deps import current_user, get_rls_session, require_role
from app.auth.jwt import AuthUser
from app.db.rls import service_session
from app.services import jobs
from app.services.extract_metadata import MIN_YEAR
from app.services.storage import get_storage

router = APIRouter()

EDIT_ROLES = ("owner", "editor")
LIST_COLUMNS = ("p.id, p.library_id, p.status, p.error, p.original_filename, p.width, p.height, p.taken_at, "
                "p.taken_at_source, p.taken_at_confidence, p.thumb_key, p.created_at, p.near_duplicate_of, "
                "coalesce(p.taken_at, p.created_at) AS sort_at")


class DatePatch(BaseModel):
    taken_at: datetime


def _item(r, storage) -> dict:
    return {
        "id": r.id,
        "library_id": r.library_id,
        "status": r.status,
        "error": r.error,
        "filename": r.original_filename,
        "width": r.width,
        "height": r.height,
        "taken_at": r.taken_at,
        "taken_at_source": r.taken_at_source,
        "taken_at_confidence": r.taken_at_confidence,
        "near_duplicate_of": r.near_duplicate_of,
        "thumbnail_url": storage.presign_get(r.thumb_key) if r.thumb_key else None,
    }


def _encode_cursor(sort_at: datetime, pid: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{sort_at.isoformat()}|{pid}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        ts, pid = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(ts), uuid.UUID(pid)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cursor") from None


@router.get("")
def list_photos(
    library_id: uuid.UUID | None = None,
    status: Literal["pending", "processing", "ready", "failed", "duplicate"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    user: AuthUser = Depends(current_user),
    session: Session = Depends(get_rls_session),
):
    """Newest first by capture time (upload time when the date is unknown). Pending uploads and
    duplicates are hidden unless asked for with ?status=."""
    if library_id:
        require_role(session, user.id, library_id, ("owner", "editor", "viewer"))
    where, params = [], {"lib": library_id, "limit": limit + 1}
    where.append("(CAST(:lib AS uuid) IS NULL OR p.library_id = CAST(:lib AS uuid))")
    if status:
        where.append("p.status = :status")
        params["status"] = status
    else:
        where.append("p.status NOT IN ('pending', 'duplicate')")
    if cursor:
        params["cts"], params["cid"] = _decode_cursor(cursor)
        where.append("(coalesce(p.taken_at, p.created_at), p.id) < (:cts, :cid)")

    rows = session.execute(
        text(f"SELECT {LIST_COLUMNS} FROM photo p WHERE {' AND '.join(where)} "
             "ORDER BY sort_at DESC, p.id DESC LIMIT :limit"), params).all()
    storage = get_storage()
    page = rows[:limit]
    return {
        "items": [_item(r, storage) for r in page],
        "next_cursor": _encode_cursor(page[-1].sort_at, page[-1].id) if len(rows) > limit else None,
    }


@router.get("/{photo_id}")
def get_photo(photo_id: uuid.UUID, user: AuthUser = Depends(current_user),
              session: Session = Depends(get_rls_session)):
    r = session.execute(
        text(f"SELECT {LIST_COLUMNS}, p.content_type, p.byte_size, p.duplicate_of, p.enriched_at, p.enrich_error, "
             "ST_AsGeoJSON(p.location) AS location FROM photo p WHERE p.id = :id"), {"id": photo_id}).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    out = _item(r, get_storage())
    out.update({"content_type": r.content_type, "byte_size": r.byte_size, "duplicate_of": r.duplicate_of,
                "enriched": r.enriched_at is not None, "enrich_error": r.enrich_error,
                "location": r.location})
    return out


@router.get("/{photo_id}/url")
def photo_url(photo_id: uuid.UUID, size: Literal["thumb", "display", "original"] = "display",
              user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """A short-lived signed link. The database lookup (under RLS) is the access check."""
    r = session.execute(
        text("SELECT original_key, display_key, thumb_key, original_filename FROM photo WHERE id = :id"),
        {"id": photo_id}).first()
    key = None if r is None else {"thumb": r.thumb_key, "display": r.display_key, "original": r.original_key}[size]
    if not key:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.storage import GET_URL_TTL_SECONDS
    filename = r.original_filename if size == "original" else None
    return {"url": get_storage().presign_get(key, filename=filename), "expires_in": GET_URL_TTL_SECONDS}


@router.post("/{photo_id}/retry")
def retry_photo(photo_id: uuid.UUID, user: AuthUser = Depends(current_user),
                session: Session = Depends(get_rls_session)):
    r = session.execute(text("SELECT library_id, status FROM photo WHERE id = :id"), {"id": photo_id}).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    require_role(session, user.id, r.library_id, EDIT_ROLES)
    if r.status != "failed":
        raise HTTPException(status_code=409, detail="Only failed photos can be retried")
    with service_session() as s:
        s.execute(text("UPDATE photo SET status = 'processing', error = NULL WHERE id = :id AND status = 'failed'"),
                  {"id": photo_id})
        jobs.enqueue(s, "process_photo", {"photo_id": str(photo_id)}, dedupe_key=f"process_photo:{photo_id}")
    return {"id": photo_id, "status": "processing"}


@router.patch("/{photo_id}")
def correct_date(photo_id: uuid.UUID, body: DatePatch, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    """Set the capture date by hand. A manual date is never overwritten by reprocessing."""
    r = session.execute(text("SELECT library_id FROM photo WHERE id = :id"), {"id": photo_id}).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    require_role(session, user.id, r.library_id, EDIT_ROLES)
    taken = body.taken_at if body.taken_at.tzinfo else body.taken_at.replace(tzinfo=timezone.utc)
    if taken.year < MIN_YEAR or taken > datetime.now(timezone.utc) + timedelta(days=1):
        raise HTTPException(status_code=422, detail=f"Date must be between {MIN_YEAR} and tomorrow")
    session.execute(
        text("UPDATE photo SET taken_at = :t, taken_at_source = 'manual', taken_at_confidence = 1.0, taken_tz = NULL "
             "WHERE id = :id"), {"t": taken, "id": photo_id})
    session.commit()
    return {"id": photo_id, "taken_at": taken, "taken_at_source": "manual"}


@router.delete("/{photo_id}", status_code=204)
def delete_photo(photo_id: uuid.UUID, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    """Deletes the row (faces and album links cascade), then queues removal of the files. If queuing fails
    the files are orphaned, never the other way round."""
    r = session.execute(
        text("SELECT library_id, original_key, display_key, thumb_key FROM photo WHERE id = :id"),
        {"id": photo_id}).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    require_role(session, user.id, r.library_id, EDIT_ROLES)
    session.execute(text("DELETE FROM photo WHERE id = :id"), {"id": photo_id})
    session.commit()
    keys = [k for k in (r.original_key, r.display_key, r.thumb_key) if k]
    with service_session() as s:
        jobs.enqueue(s, "delete_objects", {"keys": keys})
