"""
Database models for Stitchfren.
"""

from sqlalchemy import Column, String, DateTime, JSON, Float, Integer, Boolean
from sqlalchemy.sql import func
from .database import Base
import uuid


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(String, index=True, nullable=False)
    
    # Input
    input_data = Column(JSON, nullable=False)
    
    # Output
    result_data = Column(JSON, nullable=True)
    status = Column(String, default="processing")  # processing, completed, failed
    
    fabric_saved_cm = Column(Float, nullable=True)
    fabric_saved_pct = Column(Float, nullable=True)
    result_hash = Column(String, nullable=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    
    error_message = Column(String, nullable=True)


class ApiKey(Base):
    """
    Persisted API keys. Replaces the old in-memory dict, which was wiped
    on every restart/redeploy (fatal on Railway, where a git push redeploys
    and would otherwise invalidate every issued key mid-session).
    Only the SHA-256 hash is stored, never the raw key.
    """
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    hashed_key = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(String, index=True, nullable=False)
    is_active = Column(Boolean, default=True)
    usage_count = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    last_used_at = Column(DateTime(timezone=True), nullable=True)
