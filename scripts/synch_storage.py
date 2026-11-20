from app.models.photo import Photo
from sqlmodel import Session, select

from app.db.session import engine
from app.services.storage import storage_service


def cleanup_orphaned_files():
    print("🔍 Starting storage reconciliation...")

    # 1. Get all files from Supabase
    with Session(engine) as session:
        db_photos = session.exec(select(Photo.original_url)).all()
        # We extract just the actual filename part of the URL
        db_filenames = {url.split('/')[-1] for url in db_photos}
    
    print(f"✅ Found {len(db_filenames)} records in Supabase.")

    # 2. Get all files from Cloudflare R2
    # Note: You'll need to add a 'list_files' method to your storage_service
    r2_filenames = set(storage_service.list_all_files())
    print(f"✅ Found {len(r2_filenames)} files in Cloudflare R2.")

    # 3. Find the difference (Orphans)
    orphans = r2_filenames - db_filenames

    if not orphans:
        print("✨ No orphaned files found. Everything is in sync!")
        return

    print(f"⚠️ Found {len(orphans)} orphaned files. Preparing to delete...")

    # 4. Delete Orphans
    for filename in orphans:
        try:
            storage_service.delete_file(filename)
            print(f"🗑️ Deleted: {filename}")
        except Exception as e:
            print(f"❌ Failed to delete {filename}: {e}")

    print("🏁 Cleanup complete. R2 and Supabase are now synchronized.")

if __name__ == "__main__":
    cleanup_orphaned_files()