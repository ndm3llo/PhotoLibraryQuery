import mimetypes
import os

import boto3
from dotenv import load_dotenv

load_dotenv()

class StorageService:
    def __init__(self):
        # Using your exact environmental variable names
        self.s3 = boto3.client(
            service_name="s3",
            endpoint_url=os.getenv("cloudflare_endpoint"),
            aws_access_key_id=os.getenv("cloudflare_access_id"),
            aws_secret_access_key=os.getenv("cloudflare_accesss_key"),
            region_name="auto",
        )
        self.bucket = os.getenv("R2_BUCKET_NAME")
        
    def get_image_bytes(self, file_path_or_key: str) -> bytes:
        """
        Fetches raw bytes from R2. 
        Handles both full URLs and simple keys like 'originals/photo.jpg'
        """
        public_base = os.getenv("R2_PUBLIC_URL")
        
        # 1. Clean the key: remove the domain if it's a full URL
        file_key = file_path_or_key.replace(f"{public_base}/", "")
        
        # 2. Strip leading slashes (S3/R2 keys should not start with /)
        file_key = file_key.lstrip("/")

        print(f"DEBUG: Requesting key from R2: '{file_key}' in bucket '{self.bucket}'")

        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=file_key)
            return response['Body'].read()
        except Exception as e:
            print(f"S3/R2 Error: {e}")
            raise e
        
    def upload_image(self, file_data: bytes, file_name: str, folder: str = "originals") -> str:
        """
        Uploads bytes to R2 and returns the full public URL.
        - folder: 'originals' for the high-res file, 'thumbnails' for the small one.
        """
        # Create the path inside the bucket (e.g., originals/photo.jpg)
        file_key = f"{folder}/{file_name}"
        
        # Detect content type (image/jpeg, image/png, etc.)
        content_type, _ = mimetypes.guess_type(file_name)
        if not content_type:
            content_type = "image/jpeg"

        self.s3.put_object(
            Bucket=self.bucket,
            Key=file_key,
            Body=file_data,
            ContentType=content_type
        )
        
        # Get the public URL base from your env
        public_base = os.getenv("R2_PUBLIC_URL")
        
        # Return the final link to be saved in Supabase
        return f"{public_base}/{file_key}"
    
    def list_all_files(self):
        """Lists all objects in the R2 bucket."""
        response = self.s3.list_objects_v2(Bucket=self.bucket)
        if 'Contents' not in response:
            return []
        return [obj['Key'] for obj in response['Contents']]

    def delete_file(self, file_path_or_key: str):
        """
        Deletes an object. 
        Works if you pass the full URL from the DB or just the key.
        """
        public_base = os.getenv("R2_PUBLIC_URL")
        # Strip the public URL part to get the internal R2 key
        file_key = file_path_or_key.replace(f"{public_base}/", "")
        
        self.s3.delete_object(Bucket=self.bucket, Key=file_key)

    def delete_image(self, file_path_or_key: str):
        """
        Deletes an object from the R2 bucket.
        Handles both full URLs from the DB and raw keys.
        """
        public_base = os.getenv("R2_PUBLIC_URL", "")
        
        # 1. Strip the domain if a full URL was passed
        # 2. Strip any leading slashes (R2/S3 keys should not start with /)
        file_key = file_path_or_key.replace(f"{public_base}/", "").lstrip("/")

        # Using self.bucket to match your __init__ variable
        return self.s3.delete_object(Bucket=self.bucket, Key=file_key)
storage_service = StorageService()