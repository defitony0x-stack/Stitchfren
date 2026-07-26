"""
Database configuration for Stitchfren.
SQLite for local dev (default), PostgreSQL in production (Railway etc).
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./stitchfren.db")

# Railway (and Heroku-style platforms) hand out "postgres://" but
# SQLAlchemy 1.4+/2.x requires the "postgresql://" scheme - normalize it
# rather than fail cryptically on connect.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # Managed Postgres (Railway, RDS, etc.) silently drops idle connections;
    # without this, requests after a quiet period fail with
    # "server closed the connection unexpectedly" instead of reconnecting.
    pool_pre_ping=not _is_sqlite,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Creates all tables if they don't exist. There's no real Alembic
    migration wired up yet (the alembic/ folder is an empty stub), so this
    is the pragmatic fix to get a fresh deploy working. Replace with real
    migrations before this holds production data you can't afford to lose
    on a schema change.
    """
    from . import models  # noqa: F401 - ensures models are registered on Base
    Base.metadata.create_all(bind=engine)
