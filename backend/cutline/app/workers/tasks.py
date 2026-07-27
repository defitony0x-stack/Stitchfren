"""
Celery background tasks for Stitchfren.
"""

from app.core.celery_app import celery_app
from app.models.schemas import PatternRequest
from app.db.database import SessionLocal
from app.db.models import Job
from app.mcp.job import run_pattern_job
import asyncio
from typing import Dict, Any
from datetime import datetime


@celery_app.task(bind=True, name="generate_pattern_task")
def generate_pattern_task(self, request_data: Dict[str, Any], user_id: str = "default"):
    """
    Background task that performs the full pattern generation + nesting.
    Saves result to database for persistence.
    """
    db = SessionLocal()
    
    try:
        # Create initial job record
        job = Job(
            task_id=self.request.id,
            user_id=user_id,
            input_data=request_data,
            status="processing"
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        # Reconstruct Pydantic model
        request = PatternRequest(**request_data)

        # The full draft + nest + DXF + cutting-sheet pipeline now lives in
        # app/mcp/job.py, shared with the new MCP tool - asyncio.run() here
        # plays the same role the old inline asyncio.run(generate_cutting_sheet(...))
        # call did, just wrapping the whole pipeline instead of one step of it.
        result = asyncio.run(run_pattern_job(request, request_data))

        # Update job with results
        job.result_data = result
        job.status = "completed"
        job.fabric_saved_cm = result["fabric_saved_cm"]
        job.fabric_saved_pct = result["fabric_saved_pct"]
        job.result_hash = result["result_hash"]
        job.completed_at = datetime.utcnow()
        
        db.commit()

        return result

    except Exception as e:
        # Mark job as failed
        if 'job' in locals():
            job.status = "failed"
            job.error_message = str(e)
            db.commit()
        
        raise e
    finally:
        db.close()
