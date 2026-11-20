import json
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlmodel import Session

from app.auth.deps import get_rls_session
from app.services.ai import ai_service

router = APIRouter()


def _view_url(key: str | None) -> str | None:
    return f"/api/images/view/{key}" if key else None


@router.get("/")
async def search_photos(
    query: str,
    limit: int = Query(5, ge=1, le=100),
    library_id: uuid.UUID | None = None,
    session: Session = Depends(get_rls_session),
):
    """Text-to-image search across the libraries you belong to (or one of them)."""
    query_vector = ai_service.generate_text_embedding(query)

    # Row Level Security already limits this to the caller's libraries.
    rows = session.execute(
        text("""
            SELECT id, library_id, uploader_id, original_key, thumb_key, captured_at,
                   ST_AsGeoJSON(location) AS location_json,
                   1 - (clip_embedding <=> :query_vec) AS similarity
            FROM photo
            WHERE status = 'ready' AND clip_embedding IS NOT NULL
              AND (CAST(:lib AS uuid) IS NULL OR library_id = CAST(:lib AS uuid))
            ORDER BY clip_embedding <=> :query_vec
            LIMIT :limit
        """),
        {"query_vec": str(query_vector), "limit": limit, "lib": str(library_id) if library_id else None},
    ).all()

    return {
        "query": query,
        "results": [
            {
                "id": str(r.id),
                "library_id": str(r.library_id),
                "uploader_id": str(r.uploader_id),
                "view_url": _view_url(r.original_key),
                "thumbnail_url": _view_url(r.thumb_key),
                "captured_at": r.captured_at.isoformat() if r.captured_at else None,
                "location": json.loads(r.location_json) if r.location_json else None,
                "similarity_score": round(float(r.similarity), 4),
            }
            for r in rows
        ],
    }
