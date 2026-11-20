import io
import zipfile
from typing import List

from fastapi import APIRouter, Body, HTTPException, Response

from app.services.storage import storage_service

router = APIRouter()

# --- Single View (Existing) ---
@router.get("/view/{file_key:path}")
async def view_photo(file_key: str):
    try:
        image_bytes = storage_service.get_image_bytes(file_key)
        
        if file_key.lower().endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif file_key.lower().endswith(".png"):
            mime = "image/png"
        elif file_key.lower().endswith(".heic"):
            mime = "image/heic"
        else:
            mime = "application/octet-stream"

        return Response(
            content=image_bytes, 
            media_type=mime,
            headers={"Content-Disposition": "inline"}
        )
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))

# --- Batch View / Download (New) ---
@router.post("/batch-download")
async def download_batch_photos(
    file_keys: List[str] = Body(..., example=["originals/uuid1.jpg", "originals/uuid2.jpg"])
):
    """
    Accepts a list of file keys (like 'chosen_paths') and 
    returns a ZIP file containing all of them.
    """
    if not file_keys:
        raise HTTPException(status_code=400, detail="List of paths is empty")

    # Create an in-memory ZIP file
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for key in file_keys:
            try:
                # Fetch bytes from R2
                file_bytes = storage_service.get_image_bytes(key)
                # Use just the filename part for the ZIP entry
                filename = key.split("/")[-1]
                zip_file.writestr(filename, file_bytes)
            except Exception as e:
                print(f"Skipping {key} due to error: {e}")
                continue

    # Prepare the response
    zip_buffer.seek(0)
    return Response(
        content=zip_buffer.getvalue(),
        media_type="application/x-zip-compressed",
        headers={"Content-Disposition": "attachment; filename=debug_photos.zip"}
    )