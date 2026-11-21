import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlmodel import Session

from app.auth.deps import current_user, get_rls_session, require_role
from app.auth.jwt import AuthUser
from app.config import app_env
from app.db.audit import audit
from app.services.storage import get_storage

router = APIRouter()


@router.delete("/wipe-data")
async def delete_all_photos(
    library_id: uuid.UUID = Query(..., description="Library whose photos will be deleted"),
    confirm: str = Query(..., description="Type 'WIPE_PHOTOS' to confirm"),
    user: AuthUser = Depends(current_user),
    session: Session = Depends(get_rls_session),
):
    """
    DANGER: deletes every photo in ONE library (rows and R2 files). Library owners only, and only
    when APP_ENV=dev. Returns 404 in any other environment, as if the route did not exist.
    """
    if app_env() != "dev":
        raise HTTPException(status_code=404, detail="Not found")
    require_role(session, user.id, library_id, ("owner",))
    if confirm != "WIPE_PHOTOS":
        raise HTTPException(status_code=400, detail="Confirmation string incorrect. Wipe aborted.")

    rows = session.execute(
        text("SELECT original_key, display_key, thumb_key FROM photo WHERE library_id = :l"),
        {"l": library_id},
    ).all()

    deleted_files = 0
    storage = get_storage()
    for row in rows:
        for key in {k for k in row if k}:
            try:
                storage.delete(key)
                deleted_files += 1
            except Exception as e:
                print(f"R2 delete error for {key}: {e}")

    # Faces and album links go with the photo rows (ON DELETE CASCADE).
    res = session.execute(text("DELETE FROM photo WHERE library_id = :l"), {"l": library_id})
    session.commit()
    audit(user.id, "library.wipe_photos", library_id=library_id, meta={"photos": res.rowcount})
    return {"status": "success", "photos_deleted": res.rowcount, "files_deleted": deleted_files}
