import os
import uuid

from app.models.photo import Photo
from sqlmodel import Session, select  # Added select for lookups

from app.db.session import engine
from app.services.ai import ai_service
from app.services.storage import storage_service

PHOTO_FOLDER = "bsmall"

def upload_and_vectorize():
    for filename in os.listdir(PHOTO_FOLDER):
        if filename.lower().endswith((".png", ".jpg", ".jpeg")):
            file_path = os.path.join(PHOTO_FOLDER, filename)
            
            # --- STEP 1: READ FILE & CHECK FOR DUPLICATES ---
            with open(file_path, "rb") as f:
                image_bytes = f.read()
            
            # Generate the visual fingerprint
            current_phash = ai_service.generate_phash(image_bytes)
            
            with Session(engine) as session:
                # Query the DB to see if this fingerprint already exists
                statement = select(Photo).where(Photo.p_hash == current_phash)
                existing_photo = session.exec(statement).first()
                
                if existing_photo:
                    print(f"🚫 Skipping {filename}: Visual duplicate already exists (DB ID: {existing_photo.id})")
                    continue 

            #Unique
            print(f"✨ {filename} is unique. Processing...")
            
            # 1. Generate AI Vector
            vector = ai_service.generate_embedding(file_path)

            # 2. Upload to Cloudflare R2
            unique_name = f"{uuid.uuid4()}-{filename}"
            public_url = storage_service.upload_image(image_bytes, unique_name)
            
            # 3. Save to Supabase
            with Session(engine) as session:
                new_photo = Photo(
                    filename=filename, 
                    original_url=public_url,
                    preview_url=public_url,
                    p_hash=current_phash, 
                    embedding=vector 
                )
                session.add(new_photo)
                session.commit()
            
            print(f"✅ Success: {filename} uploaded.")

if __name__ == "__main__":
    upload_and_vectorize()