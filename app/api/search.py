import json  # To parse the GeoJSON string

from fastapi import APIRouter
from sqlmodel import Session, text

from app.db.session import engine
from app.services.ai import ai_service

router = APIRouter()

@router.get("/")
async def search_photos(query: str, limit: int = 5):
    # 1. Generate text embedding for the search query
    query_vector = ai_service.generate_text_embedding(query)
    
    # 2. Query Database
    with Session(engine) as session:
        # Selecting all fields except the heavy vectors/hashes
        statement = text("""
            SELECT 
                id, 
                user_id, 
                album_id, 
                storage_path, 
                thumbnail_path, 
                captured_at, 
                ST_AsGeoJSON(location) as location_json,
                1 - (clip_embedding <=> :query_vec) as similarity
            FROM photo
            ORDER BY clip_embedding <=> :query_vec
            LIMIT :limit
        """)
        
        results = session.exec(statement, params={
            "query_vec": str(query_vector), 
            "limit": limit
        }).all()

        formatted_results = []
        for r in results:
            # Extract the R2 key from the full storage URL 
            # (Assumes storage_path contains your R2_PUBLIC_URL)
            try:
                # Splits at the end of the domain to get 'originals/uuid.jpg'
                file_key = r[3].split('.dev/')[-1]
                view_url = f"http://localhost:8000/api/images/view/{file_key}"
        
            except Exception:
                view_url = r[3] # Fallback if split fails
            thumb_key = r[4].split('.dev/')[-1]
            thumb_url = f"http://localhost:8000/api/images/view/{thumb_key}"

            formatted_results.append({
                "id": str(r[0]),
                "user_id": str(r[1]),
                "album_id": str(r[2]) if r[2] else None,
                "storage_path": r[3],
                "view_url": view_url,  # Clickable in /docs
                "thumbnail_url": thumb_url,
                "thumbnail_path": r[4],
                "captured_at": r[5].isoformat() if r[5] else None,
                "location": json.loads(r[6]) if r[6] else None,
                "similarity_score": round(float(r[7]), 4)
            })

        return {
            "query": query, 
            "results": formatted_results
        }