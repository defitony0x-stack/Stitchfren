"""
Cloudflare R2 (S3-compatible) storage for generated DXF files.

Why this exists: the old code wrote DXF files to local /tmp and served them
from a separate endpoint. That works on a single VPS process, but breaks on
Railway (or any multi-instance/multi-service deploy):
  - the Celery worker and the web service are separate instances with
    separate filesystems, so the web service can never see a file the
    worker wrote.
  - even on a single service, Railway's filesystem is ephemeral - wiped on
    every redeploy/restart.

If R2 isn't configured (no env vars set), upload_dxf() returns None and the
caller falls back to the old local-/tmp behavior - useful for local/VPS
testing without needing R2 credentials yet.
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

_client = None


def _get_client():
    """Lazily construct the boto3 client so importing this module doesn't
    require boto3/credentials to be present unless R2 is actually used."""
    global _client
    if _client is not None:
        return _client

    account_id = os.getenv("R2_ACCOUNT_ID")
    access_key = os.getenv("R2_ACCESS_KEY_ID")
    secret_key = os.getenv("R2_SECRET_ACCESS_KEY")

    if not (account_id and access_key and secret_key):
        return None

    import boto3
    from botocore.config import Config

    _client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    return _client


def is_configured() -> bool:
    return _get_client() is not None


def upload_dxf(local_path: str, key_prefix: str = "dxf") -> Optional[str]:
    """
    Uploads a local DXF file to R2 and returns a URL for it, or None if R2
    isn't configured.

    Two modes, controlled by env vars:
      - R2_PUBLIC_URL set (e.g. a custom domain or r2.dev public bucket URL):
        returns a permanent public URL. Use this if the bucket is public.
      - R2_PUBLIC_URL not set: returns a presigned URL valid for
        R2_PRESIGNED_EXPIRY_SECONDS (default 7 days). Use this for a
        private bucket, which is the safer default.
    """
    client = _get_client()
    if client is None:
        return None

    bucket = os.getenv("R2_BUCKET_NAME")
    if not bucket:
        raise RuntimeError(
            "R2 credentials are set but R2_BUCKET_NAME is missing. "
            "Set R2_BUCKET_NAME in your environment."
        )

    filename = os.path.basename(local_path)
    object_key = f"{key_prefix}/{uuid.uuid4().hex[:12]}_{filename}"

    client.upload_file(
        local_path,
        bucket,
        object_key,
        ExtraArgs={"ContentType": "application/dxf"},
    )

    public_base = os.getenv("R2_PUBLIC_URL")
    if public_base:
        return f"{public_base.rstrip('/')}/{object_key}"

    expiry = int(os.getenv("R2_PRESIGNED_EXPIRY_SECONDS", str(7 * 24 * 3600)))
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": object_key},
        ExpiresIn=expiry,
    )
