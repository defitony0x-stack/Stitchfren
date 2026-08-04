"""
FastAPI application for Cut Line v2.
Production-grade refactor with Pydantic, DXF export, LLM layer, and improved nesting.
"""

from __future__ import annotations

import os
import hashlib
import json
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from pydantic import ValidationError

from ..models.schemas import (
    PatternRequest,
    PatternResponse,
    PatternStyle,
    ErrorResponse,
)
from ..workers.tasks import generate_pattern_task
from celery.result import AsyncResult
from app.core.celery_app import celery_app
from app.core.security import get_current_key
from app.services.llm_service import enhance_with_llm, parse_measurements_from_text
from app.db.database import get_db, init_db
from sqlalchemy.orm import Session
from app.mcp.server import mcp_app, mcp_app_gated


@asynccontextmanager
async def lifespan(app: FastAPI):
    # No real Alembic migrations wired up yet - see app/db/database.py
    # init_db() for the caveat. This is what makes a fresh deploy (e.g. a
    # brand new Railway Postgres) actually have a jobs/api_keys table to
    # write to, instead of failing on the first request.
    init_db()
    # fastmcp's Streamable HTTP transport needs its own session manager
    # running for the lifetime of the app (this is what makes /mcp actually
    # answer instead of 404 - see app/mcp/server.py's module docstring for
    # why mounting alone isn't enough).
    async with mcp_app.lifespan(app):
        yield


# Create FastAPI app
app = FastAPI(
    title="Stitchfren — Pattern Drafting & Nesting Agent",
    description="Production-grade ASP for OKX.AI Marketplace. From measurements to optimized cutting layout with proven fabric savings.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Real MCP protocol server (initialize / tools/list / tools/call), x402
# payment-gated per-call for the one priced tool. See app/mcp/server.py.
app.mount("/mcp", mcp_app_gated)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def root():
    """
    Bare-domain liveness check. Some marketplace/reachability probes
    (OKX's ASP review included) hit the root path before ever trying
    /mcp or /health, and a 404 there can get reported as "endpoint
    could not be reached" even though the real API is fine. Keep this
    trivial and dependency-free so it can never fail for a reason
    unrelated to whether the service is up.
    """
    return {"status": "ok", "service": "Stitchfren", "version": "2.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/api/styles")
async def list_styles():
    """
    Public, unauthenticated: just the current PatternStyle enum values and
    count. Exists so the frontend's "N drafting styles shipped" stat reads
    the real enum instead of a hardcoded number that goes stale every time
    a style gets added (see PatternStyle in app/models/schemas.py).
    """
    styles = [s.value for s in PatternStyle]
    return {"styles": styles, "count": len(styles)}


@app.post("/api/keys/generate")
async def generate_new_api_key(user_id: str = "default", db: Session = Depends(get_db)):
    """
    Generate a new API Key.
    In production, this should be protected or handled via OKX dashboard.
    """
    from app.core.security import create_api_key
    new_key = create_api_key(db, user_id=user_id)
    return {
        "api_key": new_key,
        "message": "Store this key securely. It will not be shown again.",
        "user_id": user_id
    }


@app.get("/api/jobs")
async def list_jobs(
    current_key: dict = Depends(get_current_key),
    limit: int = 20
):
    """List recent jobs for the current user."""
    from app.db.database import SessionLocal
    from app.db.models import Job
    
    db = SessionLocal()
    try:
        jobs = db.query(Job).filter(
            Job.user_id == current_key.get("user_id")
        ).order_by(Job.created_at.desc()).limit(limit).all()
        
        return [
            {
                "task_id": job.task_id,
                "status": job.status,
                "fabric_saved_cm": job.fabric_saved_cm,
                "fabric_saved_pct": job.fabric_saved_pct,
                "created_at": job.created_at,
                "completed_at": job.completed_at
            }
            for job in jobs
        ]
    finally:
        db.close()


@app.get("/api/jobs/{task_id}")
async def get_job(
    task_id: str,
    current_key: dict = Depends(get_current_key)
):
    """Get details of a specific job."""
    from app.db.database import SessionLocal
    from app.db.models import Job
    
    db = SessionLocal()
    try:
        job = db.query(Job).filter(
            Job.task_id == task_id,
            Job.user_id == current_key.get("user_id")
        ).first()
        
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        
        return {
            "task_id": job.task_id,
            "status": job.status,
            "input_data": job.input_data,
            "result_data": job.result_data,
            "fabric_saved_cm": job.fabric_saved_cm,
            "fabric_saved_pct": job.fabric_saved_pct,
            "result_hash": job.result_hash,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
            "error_message": job.error_message
        }
    finally:
        db.close()


@app.post("/api/pattern")
async def create_pattern(
    request: PatternRequest,
    current_key: dict = Depends(get_current_key)
):
    """
    Accepts pattern request and queues it as a Celery background task.
    Requires valid X-API-Key header.
    """
    task = generate_pattern_task.delay(request.model_dump(), user_id=current_key.get("user_id", "default"))
    return {
        "task_id": task.id,
        "status": "processing",
        "message": "Pattern generation started. Poll /api/status/{task_id} for results.",
        "user_id": current_key.get("user_id", "default")
    }


@app.get("/api/status/{task_id}")
async def get_task_status(
    task_id: str,
    current_key: dict = Depends(get_current_key)
):
    """
    Check status of a background pattern generation task.
    Requires valid X-API-Key header.
    """
    result = AsyncResult(task_id, app=celery_app)
    
    if result.state == 'PENDING':
        return {"task_id": task_id, "status": "pending"}
    elif result.state == 'STARTED':
        return {"task_id": task_id, "status": "processing"}
    elif result.state == 'SUCCESS':
        return {"task_id": task_id, "status": "completed", "result": result.result}
    elif result.state == 'FAILURE':
        return {"task_id": task_id, "status": "failed", "error": str(result.result)}
    else:
        return {"task_id": task_id, "status": result.state.lower()}


@app.post("/api/parse-text")
async def parse_text(request: dict):
    """Free-text to structured measurements (LLM + rule-based fallback)."""
    text = request.get("text", "")
    if not text:
        raise HTTPException(400, "text is required")

    # Try LLM first
    llm_result = await enhance_with_llm(text)
    if llm_result:
        return {"ok": True, "source": "llm", "data": llm_result}

    # Fallback
    parsed = parse_measurements_from_text(text)
    if parsed:
        return {"ok": True, "source": "rule-based", "data": parsed}

    raise HTTPException(422, "Could not parse measurements from text. Please use the structured form.")


# Simple file serving for generated DXF (demo only — use object storage in prod)
@app.get("/download/dxf/{filename}")
async def download_dxf(filename: str):
    path = f"/tmp/{filename}"
    if os.path.exists(path):
        return FileResponse(path, media_type="application/dxf", filename=filename)
    raise HTTPException(404, "File not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api.main:app", host="0.0.0.0", port=8000, reload=True)
