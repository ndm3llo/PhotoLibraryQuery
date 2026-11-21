"""Object storage (Cloudflare R2 in production, any S3-compatible server in tests).

The bucket is PRIVATE. Nothing here returns a public URL: browsers upload with short-lived presigned
PUT links and read with short-lived presigned GET links, both issued only after an RLS-checked
database lookup.

Notes:
- boto3 >= 1.36 adds CRC32 checksum headers to uploads by default; R2 rejects them on presigned URLs.
  `request_checksum_calculation="when_required"` turns that off, so the only header a browser must
  send is Content-Type (matching the CORS rule in infra/r2-cors.json).
- Keys are library-scoped: {library_id}/originals/{photo_id}, {library_id}/display/{photo_id}.webp,
  {library_id}/thumbs/{photo_id}.webp
"""
import os
from typing import Iterable
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

PUT_URL_TTL_SECONDS = 900
GET_URL_TTL_SECONDS = 600


def original_key(library_id: UUID, photo_id: UUID) -> str:
    return f"{library_id}/originals/{photo_id}"


def display_key(library_id: UUID, photo_id: UUID) -> str:
    return f"{library_id}/display/{photo_id}.webp"


def thumb_key(library_id: UUID, photo_id: UUID) -> str:
    return f"{library_id}/thumbs/{photo_id}.webp"


class Storage:
    def __init__(self, client, bucket: str):
        self.client = client
        self.bucket = bucket

    @classmethod
    def create(cls, *, endpoint_url: str, access_key: str, secret_key: str, bucket: str,
               region: str = "auto") -> "Storage":
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return cls(client, bucket)

    @classmethod
    def from_env(cls) -> "Storage":
        # Variable names kept from the prototype (including the legacy spelling of the secret).
        endpoint = os.getenv("cloudflare_endpoint")
        key_id = os.getenv("cloudflare_access_id")
        secret = os.getenv("cloudflare_accesss_key")
        bucket = os.getenv("R2_BUCKET_NAME")
        if not all([endpoint, key_id, secret, bucket]):
            raise RuntimeError("Storage is not configured (cloudflare_endpoint / _access_id / "
                               "_accesss_key / R2_BUCKET_NAME)")
        return cls.create(endpoint_url=endpoint, access_key=key_id, secret_key=secret, bucket=bucket)

    # -- links handed to browsers ------------------------------------------------------------
    def presign_put(self, key: str, content_type: str, expires: int = PUT_URL_TTL_SECONDS) -> str:
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=expires,
            HttpMethod="PUT",
        )

    def presign_get(self, key: str, expires: int = GET_URL_TTL_SECONDS, filename: str | None = None) -> str:
        params = {"Bucket": self.bucket, "Key": key}
        if filename:
            safe = filename.replace('"', "").replace("\\", "")[:200]
            params["ResponseContentDisposition"] = f'inline; filename="{safe}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)

    # -- server-side access --------------------------------------------------------------------
    def head(self, key: str) -> dict | None:
        """{'size': int, 'content_type': str} or None if the object does not exist."""
        try:
            r = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return {"size": r["ContentLength"], "content_type": r.get("ContentType")}

    def get_bytes(self, key: str, max_bytes: int | None = None) -> bytes:
        r = self.client.get_object(Bucket=self.bucket, Key=key)
        if max_bytes is not None and r["ContentLength"] > max_bytes:
            raise ValueError(f"object {key} is larger than {max_bytes} bytes")
        return r["Body"].read()

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def delete(self, key: str) -> None:
        """Deleting a missing object is not an error."""
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_many(self, keys: Iterable[str]) -> None:
        for key in {k for k in keys if k}:
            self.delete(key)


_storage: Storage | None = None


def get_storage() -> Storage:
    """Process-wide storage client, created on first use (so importing this module never needs credentials)."""
    global _storage
    if _storage is None:
        _storage = Storage.from_env()
    return _storage


def set_storage(storage: Storage | None) -> None:
    """Override the storage client (tests)."""
    global _storage
    _storage = storage
