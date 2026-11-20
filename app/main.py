from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import (
    base,
    delete_all,
    images,
    search,
    search_face,
    search_faces_in_db,
    upload,
    upload_folder,
)
from app.db.session import init_db
from app.services.ai import ai_service  # noqa: F401  (eager model load at startup)
from app.services.storage import storage_service  # noqa: F401  (eager init)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    print("Connecting to Supabase and checking tables...")
    init_db()
    print("Database initialization complete.")
    
    yield  # This is where the app runs
    
    # --- Shutdown Logic (Optional) ---
    print("Shutting down...")



app = FastAPI(lifespan=lifespan)
# --- System & Core ---
app.include_router(base.router, tags=["System"])

# --- General Photo Access ---
# This covers single viewing and your new batch ZIP downloader
app.include_router(images.router, prefix="/api/images", tags=["Images"])

# --- Uploading ---
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(upload_folder.router, prefix="/api/folder", tags=["Upload"])

# --- Searching ---
app.include_router(search.router, prefix="/api/search", tags=["Search"])
app.include_router(search_face.router, prefix="/api/search-face", tags=["Search"])

# --- Debugging ---
# The one that returns chosen_paths vs not_chosen_paths
app.include_router(search_faces_in_db.router, prefix="/api/debug", tags=["Search"])

#delete_all
app.include_router(delete_all.router, prefix="/api/admin", tags=["Admin"])