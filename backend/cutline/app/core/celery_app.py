"""
Celery application instance for Stitchfren.

Shared by app/api/main.py (to poll AsyncResult) and app/workers/tasks.py
(to register the background task). Railway's Redis plugin injects
REDIS_URL automatically when attached; CELERY_BROKER_URL/CELERY_RESULT_BACKEND
are accepted too for setups that separate broker and backend, or for local
dev against a plain Redis container.
"""

from __future__ import annotations

import os
from celery import Celery

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BROKER_URL = os.getenv("CELERY_BROKER_URL", REDIS_URL)
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)

celery_app = Celery(
    "stitchfren",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Without this, a job that finishes right as the worker restarts (a
    # Railway redeploy mid-task) can leave the DB row correct but the
    # AsyncResult permanently PENDING - track_started at least gives the
    # API an honest "processing" instead of "pending" while it's running.
    task_track_started=True,
    # Long fabric-nesting jobs shouldn't hold a worker slot forever if
    # something hangs (e.g. a pathological piece set failing to converge).
    task_time_limit=600,
    task_soft_time_limit=540,
    result_expires=86400,
)
