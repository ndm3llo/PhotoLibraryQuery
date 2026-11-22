from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import (
    base,
    delete_all,
    libraries,
    me,
    people,
    photos,
    search,
    search_face,
    search_faces_in_db,
    uploads,
    web,
)
from app.db.session import init_db
from app.services.ai import ai_service  # noqa: F401  (eager model load at startup)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup Logic ---
    print("Connecting to Supabase...")
    init_db()
    print("Database initialization complete.")
    
    yield  # This is where the app runs
    
    # --- Shutdown Logic (Optional) ---
    print("Shutting down...")



app = FastAPI(lifespan=lifespan)
# --- System & Core ---
app.include_router(base.router, tags=["System"])
app.include_router(web.router, tags=["System"])

# --- Accounts, libraries, invites (all require sign-in) ---
app.include_router(me.router, prefix="/api/me", tags=["Account"])
app.include_router(libraries.router, prefix="/api/libraries", tags=["Libraries"])
app.include_router(libraries.invites_router, prefix="/api/invites", tags=["Libraries"])

# --- Photos: direct-to-storage uploads, browsing, signed image links ---
app.include_router(uploads.router, prefix="/api/uploads", tags=["Photos"])
app.include_router(photos.router, prefix="/api/photos", tags=["Photos"])

# --- People: name faces, nicknames, merge duplicates ---
app.include_router(people.router, prefix="/api/people", tags=["People"])

# --- Searching ---
app.include_router(search.router, prefix="/api/search", tags=["Search"])
app.include_router(search_face.router, prefix="/api/search-face", tags=["Search"])

# --- Debugging ---
# The one that returns chosen_paths vs not_chosen_paths
app.include_router(search_faces_in_db.router, prefix="/api/debug", tags=["Search"])

#delete_all
app.include_router(delete_all.router, prefix="/api/admin", tags=["Admin"])