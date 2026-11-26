"""Browse, view, correct and delete photos. All reads and writes go through Row Level Security."""
import base64
import logging
import re
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
from app.services.clean_copy import EXTENSIONS, make_clean_copy
from app.services.imaging import InvalidImage
from app.services.storage import GET_URL_TTL_SECONDS, download_key, download_keys, get_storage

router = APIRouter()
logger = logging.getLogger(__name__)

EDIT_ROLES = ("owner", "editor")
# Dates people type in can be much older than anything a camera recorded: scanned family prints, for example.
MANUAL_MIN_YEAR = 1850
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
                "p.taken_tz, p.taken_at_source, p.taken_at_confidence, p.thumb_key, p.display_key, p.created_at, "
                "p.near_duplicate_of, "
                f"{SORT_AT} AS sort_at, "
                f"{TAKEN_LOCAL} AS taken_local")


class DatePatch(BaseModel):
    taken_at: datetime


# The untouched original, and where a photo was taken, are for the album's owners and the person who added it (who
# has them already). Everyone else gets the cleaned copy and no location.
SEES_ORIGINAL = ("(p.uploader_id = :me OR EXISTS (SELECT 1 FROM library_member o WHERE o.library_id = p.library_id "
                 "AND o.profile_id = :me AND o.role = 'owner'))")


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
        "added_at": r.created_at,
        "near_duplicate_of": r.near_duplicate_of,
        "thumbnail_url": storage.presign_get(r.thumb_key) if r.thumb_key else None,
        # Sent with the list so that opening a photo needs no extra request (signing happens locally and is cheap).
        "display_url": storage.presign_get(r.display_key) if r.display_key else None,
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
        year: int | None = Query(None, ge=MANUAL_MIN_YEAR, le=9999),
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
             f"CASE WHEN {SEES_ORIGINAL} THEN ST_AsGeoJSON(p.location) END AS location FROM photo p WHERE p.id = :id"),
        {"id": photo_id, "me": user.id}).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    out = _item(r, get_storage())
    out.update({"content_type": r.content_type, "byte_size": r.byte_size, "duplicate_of": r.duplicate_of,
                "enriched": r.enriched_at is not None, "enrich_error": r.enrich_error,
                "location": r.location})
    return out


@router.get("/{photo_id}/details")
def photo_details(photo_id: uuid.UUID, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    """What the viewer shows under a photo: who is in it (left to right), who added it and when, and whether the
    caller may type in the date taken. The database lookup (under RLS) is the access check."""
    photo = session.execute(text(
        "SELECT p.width, p.height, p.uploader_id, p.created_at, pr.display_name AS uploader, "
        f"(m.role = ANY(:editors) AND (NOT {DATED} OR p.uploader_id = :me)) AS can_set_date FROM photo p "
        "JOIN library_member m ON m.library_id = p.library_id AND m.profile_id = :me "
        "LEFT JOIN profile pr ON pr.id = p.uploader_id WHERE p.id = :id"),
        {"id": photo_id, "me": user.id, "editors": list(EDIT_ROLES)}).first()
    if photo is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    people = session.execute(text("""
        SELECT * FROM (
            -- Each person once, by their largest face in this photo.
            SELECT DISTINCT ON (x.id) x.id, x.display_name, x.qualifier, f.bbox
              FROM face f JOIN person x ON x.id = f.person_id
             WHERE f.photo_id = :id AND NOT x.recognition_opt_out
             ORDER BY x.id, (f.bbox[3] - f.bbox[1]) DESC NULLS LAST) found
         ORDER BY found.bbox[1] NULLS LAST, lower(found.display_name), found.id"""), {"id": photo_id}).all()
    return {
        "width": photo.width,
        "height": photo.height,
        "people": [{"id": r.id, "display_name": r.display_name, "qualifier": r.qualifier, "bbox": r.bbox}
                   for r in people],
        "added_by": {"name": photo.uploader or "Someone who left", "is_me": photo.uploader_id == user.id},
        "added_at": photo.created_at,
        "can_set_date": bool(photo.can_set_date),
    }


def _save_name(original: str | None, extension: str) -> str:
    """The original file's name, with the ending that matches the copy, limited to characters every device accepts."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", original or "")
    stem = re.sub(r"[^A-Za-z0-9 ._()-]+", "_", stem).strip(" ._")[:80]
    return f"{stem or 'photo'}.{extension}"


def _clean_copy(session: Session, photo_id: uuid.UUID) -> dict:
    """A download link to the photo without its hidden details, making the copy the first time. The database lookup
    (under RLS) is the access check."""
    r = session.execute(text(
        f"SELECT p.library_id, p.status, p.original_key, p.original_filename, p.taken_tz, {TAKEN_LOCAL} AS taken_local "
        "FROM photo p WHERE p.id = :id"), {"id": photo_id}).first()
    if r is None or r.status != "ready" or not r.original_key:
        raise HTTPException(status_code=404, detail="Photo not found")
    session.commit()  # hand the database connection back while the file is being worked on
    storage = get_storage()
    key = download_key(r.library_id, photo_id)
    made = storage.head(key)
    if made is None:
        try:
            data, content_type = make_clean_copy(storage.get_bytes(r.original_key), r.taken_local, r.taken_tz)
        except InvalidImage:
            raise HTTPException(status_code=422, detail="This photo can't be saved.") from None
        storage.put_bytes(key, data, content_type)
        if session.execute(text("SELECT 1 FROM photo WHERE id = :id"), {"id": photo_id}).first() is None:
            storage.delete(key)  # deleted while we worked: leave nothing behind
            raise HTTPException(status_code=404, detail="Photo not found")
    else:
        content_type = made["content_type"]
    name = _save_name(r.original_filename, EXTENSIONS.get(content_type, "jpg"))
    return {"url": storage.presign_get(key, filename=name, download=True), "filename": name,
            "content_type": content_type}


@router.post("/{photo_id}/save-copy")
def save_copy(photo_id: uuid.UUID, user: AuthUser = Depends(current_user),
              session: Session = Depends(get_rls_session)):
    """What "Save photo" and "Download" use: the original picture with the date taken, and without location, camera
    or other hidden details. Any member may save photos, viewers included."""
    return _clean_copy(session, photo_id)


@router.get("/{photo_id}/url")
def photo_url(photo_id: uuid.UUID, size: Literal["thumb", "display", "original"] = "display",
              user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """A short-lived signed link. The database lookup (under RLS) is the access check. Asking for the original gives
    the untouched file to owners and the uploader, and the cleaned copy to everyone else."""
    r = session.execute(
        text(f"SELECT p.original_key, p.display_key, p.thumb_key, p.original_filename, {SEES_ORIGINAL} AS full_access "
             "FROM photo p WHERE p.id = :id"), {"id": photo_id, "me": user.id}).first()
    if r is not None and size == "original" and not r.full_access:
        return {"url": _clean_copy(session, photo_id)["url"], "expires_in": GET_URL_TTL_SECONDS}
    key = None if r is None else {"thumb": r.thumb_key, "display": r.display_key, "original": r.original_key}[size]
    if not key:
        raise HTTPException(status_code=404, detail="Not found")
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
    """Type in the date a photo was taken. Owners and editors may add one where it is missing. Changing a date the
    photo already has (from the camera, the file name, or typed in) is for the person who added it. Viewers can do
    neither. A manual date is never overwritten by reprocessing."""
    r = session.execute(text(
        f"SELECT p.library_id, p.uploader_id, {DATED} AS dated FROM photo p WHERE p.id = :id"),
        {"id": photo_id}).first()
    if r is None:
        raise HTTPException(status_code=404, detail="Photo not found")
    require_role(session, user.id, r.library_id, EDIT_ROLES)
    if r.dated and r.uploader_id != user.id:
        raise HTTPException(status_code=403, detail="Only the person who added this photo can change its date")
    taken = body.taken_at if body.taken_at.tzinfo else body.taken_at.replace(tzinfo=timezone.utc)
    if taken.year < MANUAL_MIN_YEAR or taken > datetime.now(timezone.utc) + timedelta(days=1):
        raise HTTPException(status_code=422, detail=f"Date must be between {MANUAL_MIN_YEAR} and today")
    session.execute(
        text("UPDATE photo SET taken_at = :t, taken_at_source = 'manual', taken_at_confidence = 1.0, taken_tz = NULL "
             "WHERE id = :id"), {"t": taken, "id": photo_id})
    session.commit()
    # A saved copy carries the old date. Remove it now so the next save makes a new one; the queued job covers a
    # storage hiccup.
    old_copy = download_key(r.library_id, photo_id)
    with service_session() as s:
        jobs.enqueue(s, "delete_objects", {"keys": [old_copy]})
    try:
        get_storage().delete(old_copy)
    except Exception:
        logger.exception("could not remove the saved copy of photo %s yet", photo_id)
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
    keys = [k for k in (r.original_key, r.display_key, r.thumb_key, *download_keys(r.library_id, photo_id)) if k]
    with service_session() as s:
        jobs.enqueue(s, "delete_objects", {"keys": keys})
