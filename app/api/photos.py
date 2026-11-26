"""Browse, view, correct and delete photos. All reads and writes go through Row Level Security."""
import base64
import uuid
from datetime import date, datetime, timedelta, timezone
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
# A file's own timestamp says when it was saved or copied, not when the photo was taken, so such photos (and the
# unreliable "legacy" dates) count as having no date. The stored value is kept.
DATED = "(p.taken_at IS NOT NULL AND coalesce(p.taken_at_source, '') NOT IN ('file_mtime', 'legacy'))"
# Wall-clock time where the photo was taken: the instant shifted by the camera's recorded UTC offset (no offset
# recorded = the stored time already is the wall-clock time). NULL when undated.
# Every date filter MUST compare this, never the UTC date.
TAKEN_LOCAL = (f"(CASE WHEN {DATED} THEN "
               "p.taken_at AT TIME ZONE CAST(coalesce(p.taken_tz, '+00:00') AS interval) END)")
# Newest first by capture date, then the undated photos, newest upload first (moved far into the past so one value
# orders both). Photos still being processed have no date yet and stay near the top.
SORT_AT = (f"(CASE WHEN {DATED} THEN p.taken_at WHEN p.status = 'ready' THEN p.created_at - interval '1000 years' "
           "ELSE p.created_at END)")
LIST_COLUMNS = ("p.id, p.library_id, p.status, p.error, p.original_filename, p.width, p.height, p.taken_at, "
                "p.taken_tz, p.taken_at_source, p.taken_at_confidence, p.thumb_key, p.created_at, p.near_duplicate_of, "
                f"{SORT_AT} AS sort_at, "
                f"{TAKEN_LOCAL} AS taken_local")


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
        "taken_local": r.taken_local,
        "taken_tz": r.taken_tz,
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


class PhotoFilters:
    """Which photos to show. Every filter given must hold (people AND dates AND who added them)."""

    def __init__(
        self,
        person: list[uuid.UUID] = Query(default=[], max_length=20, description="Repeat for several people"),
        match: Literal["all", "any"] = Query("all", description="all = everyone is in the same photo"),
        year: int | None = Query(None, ge=MIN_YEAR, le=9999),
        date_from: date | None = None,
        date_to: date | None = None,
        on_day: str | None = Query(None, pattern=r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$",
                                   description="MM-DD in any year"),
        undated: bool = False,
        uploader: list[uuid.UUID] = Query(default=[], max_length=50),
    ):
        self.person, self.match, self.year = sorted(set(person)), match, year
        self.date_from, self.date_to, self.on_day, self.undated = date_from, date_to, on_day, undated
        self.uploader = sorted(set(uploader))

    def clauses(self) -> tuple[list[str], dict]:
        where, params = [], {}
        if self.person:
            chosen = "f.photo_id = p.id AND f.person_id = ANY(CAST(:persons AS uuid[]))"
            params["persons"] = [str(i) for i in self.person]
            if self.match == "any":
                where.append(f"EXISTS (SELECT 1 FROM face f WHERE {chosen})")
            else:
                where.append(f"(SELECT count(DISTINCT f.person_id) FROM face f WHERE {chosen}) = :person_count")
                params["person_count"] = len(self.person)
        if self.year is not None:
            where.append(f"extract(year FROM {TAKEN_LOCAL}) = :year")
            params["year"] = self.year
        if self.date_from:
            where.append(f"CAST({TAKEN_LOCAL} AS date) >= :date_from")
            params["date_from"] = self.date_from
        if self.date_to:
            where.append(f"CAST({TAKEN_LOCAL} AS date) <= :date_to")
            params["date_to"] = self.date_to
        if self.on_day:
            where.append(f"to_char({TAKEN_LOCAL}, 'MM-DD') = :on_day")
            params["on_day"] = self.on_day
        if self.undated:
            where.append(f"NOT {DATED}")
        if self.uploader:
            where.append("p.uploader_id = ANY(CAST(:uploaders AS uuid[]))")
            params["uploaders"] = [str(i) for i in self.uploader]
        return where, params


@router.get("/count")
def count_photos(library_id: uuid.UUID, filters: PhotoFilters = Depends(),
                 user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """How many ready photos match, shown before the filters are applied."""
    require_role(session, user.id, library_id, ("owner", "editor", "viewer"))
    where, params = filters.clauses()
    where += ["p.library_id = :lib", "p.status = 'ready'"]
    total = session.execute(text(f"SELECT count(*) FROM photo p WHERE {' AND '.join(where)}"),
                            {**params, "lib": library_id}).scalar()
    return {"count": total}


@router.get("/filters")
def filter_choices(library_id: uuid.UUID, user: AuthUser = Depends(current_user),
                   session: Session = Depends(get_rls_session)):
    """What there is to filter by: named people (with a face to recognize them by), years, and who added photos."""
    require_role(session, user.id, library_id, ("owner", "editor", "viewer"))
    params = {"lib": library_id}
    people = session.execute(text("""
        WITH named AS (
            SELECT f.person_id, f.bbox, f.person_source, p.id AS photo_id, p.thumb_key, p.width, p.height
              FROM face f JOIN photo p ON p.id = f.photo_id AND p.status = 'ready'
             WHERE f.library_id = :lib AND f.person_id IS NOT NULL),
        counts AS (SELECT person_id, count(DISTINCT photo_id) AS photos FROM named GROUP BY person_id),
        -- The face to show: one a person labelled, as large as possible within its photo.
        best AS (
            SELECT DISTINCT ON (person_id) person_id, bbox, thumb_key, width, height FROM named
             ORDER BY person_id, (thumb_key IS NULL), (person_source IS NOT DISTINCT FROM 'auto'),
                      CAST(bbox[3] - bbox[1] AS float) / greatest(width, 1) DESC NULLS LAST, photo_id)
        SELECT x.id, x.display_name, x.qualifier, c.photos, b.bbox, b.thumb_key, b.width, b.height
          FROM person x JOIN counts c ON c.person_id = x.id JOIN best b ON b.person_id = x.id
         WHERE x.library_id = :lib AND NOT x.recognition_opt_out
         ORDER BY c.photos DESC, lower(x.display_name), x.id"""), params).all()
    years = session.execute(text(
        f"SELECT CAST(extract(year FROM {TAKEN_LOCAL}) AS int) AS year, count(*) AS photos FROM photo p "
        "WHERE p.library_id = :lib AND p.status = 'ready' GROUP BY 1 ORDER BY 1 DESC NULLS LAST"), params).all()
    uploaders = session.execute(text(
        "SELECT p.uploader_id AS id, pr.display_name, count(*) AS photos FROM photo p "
        # Someone who has left is no longer visible to the others, but their photos remain.
        "LEFT JOIN profile pr ON pr.id = p.uploader_id "
        "WHERE p.library_id = :lib AND p.status = 'ready' GROUP BY 1, 2 ORDER BY 3 DESC, 2, 1"), params).all()
    storage = get_storage()
    return {
        "people": [{"id": r.id, "display_name": r.display_name, "qualifier": r.qualifier, "photos": r.photos,
                    "face": {"thumbnail_url": storage.presign_get(r.thumb_key) if r.thumb_key else None,
                             "bbox": r.bbox, "width": r.width, "height": r.height}} for r in people],
        "years": [{"year": r.year, "photos": r.photos} for r in years if r.year is not None],
        "undated": sum(r.photos for r in years if r.year is None),
        "uploaders": [{"id": r.id, "name": r.display_name or "Someone who left", "photos": r.photos,
                       "is_me": r.id == user.id} for r in uploaders],
    }


@router.get("")
def list_photos(
    library_id: uuid.UUID | None = None,
    status: Literal["pending", "processing", "ready", "failed", "duplicate"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    filters: PhotoFilters = Depends(),
    user: AuthUser = Depends(current_user),
    session: Session = Depends(get_rls_session),
):
    """Newest first by capture date, with undated photos after them. Pending uploads and
    duplicates are hidden unless asked for with ?status=."""
    if library_id:
        require_role(session, user.id, library_id, ("owner", "editor", "viewer"))
    where, params = [], {"lib": library_id, "limit": limit + 1}
    where.append("(CAST(:lib AS uuid) IS NULL OR p.library_id = CAST(:lib AS uuid))")
    chosen, chosen_params = filters.clauses()
    where += chosen
    params.update(chosen_params)
    if status:
        where.append("p.status = :status")
        params["status"] = status
    elif chosen:
        where.append("p.status = 'ready'")  # matches the count shown before filtering
    else:
        where.append("p.status NOT IN ('pending', 'duplicate')")
    if cursor:
        params["cts"], params["cid"] = _decode_cursor(cursor)
        where.append(f"({SORT_AT}, p.id) < (:cts, :cid)")

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
