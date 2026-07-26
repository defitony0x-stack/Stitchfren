"""
Security and Authentication for Stitchfren.
API keys are persisted in Postgres/SQLite (app.db.models.ApiKey), not an
in-memory dict - a plain dict is wiped on every restart or redeploy, which
is fatal on a platform like Railway where a git push redeploys the service.
"""

from fastapi import HTTPException, Security, Depends
from fastapi.security.api_key import APIKeyHeader
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from typing import Optional
import secrets
import hashlib

from app.db.database import get_db
from app.db.models import ApiKey

API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def generate_api_key() -> str:
    """Generate a secure random API key."""
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    """Hash the API key for storage (basic security)."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def create_api_key(db: Session, user_id: str = "default") -> str:
    """Create and persist a new API key. Returns the raw key (shown once)."""
    raw_key = generate_api_key()
    hashed_key = hash_api_key(raw_key)

    key_row = ApiKey(hashed_key=hashed_key, user_id=user_id, is_active=True, usage_count=0)
    db.add(key_row)
    db.commit()

    return raw_key


def validate_api_key(db: Session, api_key: Optional[str]) -> dict:
    """Validate API key against the DB and return key info as a dict
    (kept dict-shaped so existing call sites like current_key.get('user_id')
    don't need to change)."""
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API Key. Please provide X-API-Key header."
        )

    hashed_key = hash_api_key(api_key)
    key_row = db.query(ApiKey).filter(ApiKey.hashed_key == hashed_key).first()

    if key_row is None:
        raise HTTPException(status_code=401, detail="Invalid API Key")

    if not key_row.is_active:
        raise HTTPException(status_code=403, detail="API Key has been deactivated")

    key_row.usage_count = (key_row.usage_count or 0) + 1
    key_row.last_used_at = func.now()
    db.commit()

    return {
        "user_id": key_row.user_id,
        "is_active": key_row.is_active,
        "usage_count": key_row.usage_count,
    }


def get_current_key(
    api_key: str = Security(API_KEY_HEADER),
    db: Session = Depends(get_db),
) -> dict:
    """FastAPI dependency to get current authenticated key info."""
    return validate_api_key(db, api_key)
