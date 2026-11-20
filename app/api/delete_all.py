from fastapi import APIRouter, HTTPException, Query
from sqlmodel import Session, select, text

from app.db.session import engine
from app.models.model import Photo
from app.services.storage import storage_service

router = APIRouter()

@router.delete("/wipe-data")
async def delete_all_photos(
    confirm: str = Query(..., description="Type 'WIPE_PHOTOS' to confirm")
):
    """
    DANGER: Deletes all Photo and PhotoFace records from DB and files from R2.
    Leaves User Profiles and Albums intact.
    """
    if confirm != "WIPE_PHOTOS":
        raise HTTPException(
            status_code=400, 
            detail="Confirmation string incorrect. Wipe aborted."
        )

    try:
        with Session(engine) as session:
            # 1. Fetch all records to get storage paths before deleting
            photos = session.exec(select(Photo)).all()
            
            if not photos:
                return {"message": "No photos found. System is already clean."}

            # 2. Delete files from Cloudflare R2
            count = 0
            for photo in photos:
                # Extract the keys from the full URLs
                # 'https://.../originals/uuid.jpg' -> 'originals/uuid.jpg'
                orig_key = photo.storage_path.split(".dev/")[-1]
                thumb_key = photo.thumbnail_path.split(".dev/")[-1]
                
                try:
                    storage_service.delete_image(orig_key)
                    storage_service.delete_image(thumb_key)
                    count += 1
                except Exception as e:
                    print(f"R2 Delete Error for {orig_key}: {e}")

            # 3. Wipe the DB Tables (CASCADE handles the PhotoFace links)
            # This leaves 'Profile' and 'Album' tables exactly as they are.
            session.execute(text("TRUNCATE TABLE photo RESTART IDENTITY CASCADE;"))
            session.commit()

        return {
            "status": "success",
            "message": f"Successfully deleted {count} photo files and wiped DB records.",
            "note": "User profiles were not affected."
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wipe failed: {str(e)}")