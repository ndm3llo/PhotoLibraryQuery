"""Photo ingestion pipeline, run by the worker.

Stage 1  process_photo: makes an uploaded photo USABLE. Downloads the original, validates it by content,
         resolves the capture date, builds the display copy and thumbnail, hashes it, detects duplicates,
         and marks it `ready`. Fast, and needs no ML models.
Stage 2  enrich_photo: adds the search vector (CLIP) and face vectors. Separate and re-runnable so a model
         change (Phase 4) can re-embed old photos without re-importing them. A photo is fully usable
         without it; a failure here only sets `enrich_error`.

Both handlers are idempotent: the queue delivers at-least-once, so a re-run after a crash must be safe.
Originals are stored byte-for-byte and never rewritten.
"""
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.rls import service_session
from app.services import extract_metadata, imaging, jobs
from app.services.storage import Storage, display_key, thumb_key

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
NEAR_DUPLICATE_MAX_DISTANCE = 4  # Hamming distance between 64-bit perceptual hashes


class PermanentJobError(Exception):
    """Retrying cannot help (corrupt file, wrong type, too large, original missing)."""


@dataclass
class Enrichers:
    clip: Callable[[bytes], list[float]]
    faces: Callable[[bytes], list[list[float]]]
    clip_model: str = "clip-ViT-B-32"
    face_model: str = "facenet512"


def default_enrichers() -> Enrichers:
    """Imported lazily: loading these pulls in torch/tensorflow, which only the worker should pay for."""
    from app.services.ai import ai_service
    from app.services.img_face_recognition import extract_face_embeddings
    return Enrichers(clip=ai_service.generate_embedding_from_bytes, faces=extract_face_embeddings)


@dataclass
class Context:
    storage: Storage
    enrichers: Callable[[], Enrichers] = default_enrichers


# ------------------------------------------------------------------------------- stage 1

def process_photo(payload: dict, ctx: Context) -> str:
    """Returns 'ready', 'duplicate' or 'missing'."""
    photo_id = uuid.UUID(payload["photo_id"])
    with service_session() as s:
        row = s.execute(
            text("SELECT library_id, original_key, original_filename, client_last_modified "
                 "FROM photo WHERE id = :id"), {"id": photo_id}).first()
    if row is None:
        return "missing"  # deleted while queued
    library_id, original, filename, client_mtime = row

    if not original:
        raise PermanentJobError("photo has no original_key")
    try:
        data = ctx.storage.get_bytes(original, max_bytes=MAX_UPLOAD_BYTES)
    except ValueError as e:
        raise PermanentJobError(str(e)) from e
    except Exception as e:
        if "NoSuchKey" in str(e) or "404" in str(e):
            raise PermanentJobError("original object is missing from storage") from e
        raise  # transient storage error: retry

    try:
        img = imaging.open_image(data)
        derivs = imaging.make_derivatives(img)
        phash = imaging.compute_phash(img)
    except imaging.InvalidImage as e:
        raise PermanentJobError(str(e)) from e

    sha = imaging.sha256_hex(data)
    exif = extract_metadata.read_exif(img)
    taken = extract_metadata.resolve_taken_at(exif, filename, client_mtime)
    wkt = extract_metadata.extract_gps_wkt(exif)

    # -- exact duplicate within this library: keep the first, mark this one, free its storage
    with service_session() as s:
        dup = s.execute(
            text("SELECT id FROM photo WHERE library_id = :l AND content_sha256 = :h AND status = 'ready' "
                 "AND id <> :id LIMIT 1"), {"l": library_id, "h": sha, "id": photo_id}).scalar()
        if dup:
            return _mark_duplicate(s, photo_id, dup, original, ctx)

    # -- near duplicate (resized/re-compressed copy): flag, never drop
    with service_session() as s:
        near = s.execute(
            text("SELECT id FROM photo WHERE library_id = :l AND status = 'ready' AND id <> :id "
                 "AND phash IS NOT NULL AND bit_count((phash # :h)::bit(64)) <= :d ORDER BY id LIMIT 1"),
            {"l": library_id, "id": photo_id, "h": phash, "d": NEAR_DUPLICATE_MAX_DISTANCE}).scalar()

    d_key, t_key = display_key(library_id, photo_id), thumb_key(library_id, photo_id)
    ctx.storage.put_bytes(d_key, derivs.display, "image/webp")
    ctx.storage.put_bytes(t_key, derivs.thumb, "image/webp")

    try:
        with service_session() as s:
            s.execute(
                text("""
                    UPDATE photo SET
                        status = 'ready', error = NULL,
                        display_key = :dk, thumb_key = :tk, width = :w, height = :h,
                        content_sha256 = :sha, phash = :phash, near_duplicate_of = :near,
                        taken_at = CASE WHEN taken_at_source = 'manual' THEN taken_at ELSE :taken END,
                        taken_at_source = CASE WHEN taken_at_source = 'manual' THEN taken_at_source ELSE :src END,
                        taken_at_confidence = CASE WHEN taken_at_source = 'manual' THEN taken_at_confidence ELSE :conf END,
                        taken_tz = CASE WHEN taken_at_source = 'manual' THEN taken_tz ELSE :tz END,
                        location = ST_GeogFromText(:wkt)
                    WHERE id = :id"""),
                {"id": photo_id, "dk": d_key, "tk": t_key, "w": derivs.width, "h": derivs.height,
                 "sha": sha, "phash": phash, "near": near,
                 "taken": taken.value if taken else None, "src": taken.source if taken else None,
                 "conf": taken.confidence if taken else None, "tz": taken.tz if taken else None,
                 "wkt": f"SRID=4326;{wkt}" if wkt else None})
    except IntegrityError:
        # Two identical uploads raced past the duplicate check; the unique index let exactly one win.
        with service_session() as s:
            dup = s.execute(
                text("SELECT id FROM photo WHERE library_id = :l AND content_sha256 = :h AND status <> 'duplicate' "
                     "AND id <> :id LIMIT 1"), {"l": library_id, "h": sha, "id": photo_id}).scalar()
            ctx.storage.delete_many([d_key, t_key])
            return _mark_duplicate(s, photo_id, dup, original, ctx)

    with service_session() as s:
        jobs.enqueue(s, "enrich_photo", {"photo_id": str(photo_id)}, dedupe_key=f"enrich_photo:{photo_id}")
    return "ready"


def _mark_duplicate(session, photo_id, original_id, original_key, ctx: Context) -> str:
    session.execute(
        text("UPDATE photo SET status = 'duplicate', duplicate_of = :d, error = NULL, original_key = NULL "
             "WHERE id = :id"), {"id": photo_id, "d": original_id})
    session.commit()
    ctx.storage.delete(original_key)
    return "duplicate"


# ------------------------------------------------------------------------------- stage 2

def enrich_photo(payload: dict, ctx: Context) -> str:
    photo_id = uuid.UUID(payload["photo_id"])
    with service_session() as s:
        row = s.execute(text("SELECT library_id, display_key, status FROM photo WHERE id = :id"),
                        {"id": photo_id}).first()
    if row is None or row.status != "ready" or not row.display_key:
        return "skipped"

    enrichers = ctx.enrichers()
    data = ctx.storage.get_bytes(row.display_key)  # the 2048px copy: plenty for models, much smaller
    vector = enrichers.clip(data)
    faces = enrichers.faces(data)

    with service_session() as s:
        # Replace this model's faces atomically, so re-running never duplicates them.
        s.execute(text("DELETE FROM face WHERE photo_id = :p AND model_version = :m"),
                  {"p": photo_id, "m": enrichers.face_model})
        for emb in faces:
            s.execute(
                text("INSERT INTO face (id, photo_id, library_id, face_embedding, model_version) "
                     "VALUES (:id, :p, :l, CAST(:e AS vector), :m)"),
                {"id": uuid.uuid4(), "p": photo_id, "l": row.library_id, "e": json.dumps(list(map(float, emb))),
                 "m": enrichers.face_model})
        s.execute(
            text("UPDATE photo SET clip_embedding = CAST(:v AS vector), enriched_at = now(), enrich_error = NULL "
                 "WHERE id = :id"),
            {"id": photo_id, "v": json.dumps(list(map(float, vector)))})
    return "enriched"


def delete_objects(payload: dict, ctx: Context) -> str:
    ctx.storage.delete_many(payload.get("keys", []))
    return "deleted"


HANDLERS: dict[str, Callable[[dict, Context], str]] = {
    "process_photo": process_photo,
    "enrich_photo": enrich_photo,
    "delete_objects": delete_objects,
}


def on_dead(job: jobs.Job, error: str) -> None:
    """A job ran out of attempts. Surface that on the photo so the UI can show it and offer a retry."""
    try:
        photo_id = uuid.UUID(job.payload["photo_id"])
    except (KeyError, ValueError):
        return
    with service_session() as s:
        if job.kind == "process_photo":
            s.execute(text("UPDATE photo SET status = 'failed', error = :e WHERE id = :id AND status <> 'ready'"),
                      {"id": photo_id, "e": error[:500]})
        elif job.kind == "enrich_photo":
            s.execute(text("UPDATE photo SET enrich_error = :e WHERE id = :id"), {"id": photo_id, "e": error[:500]})
