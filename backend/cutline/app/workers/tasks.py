"""
Celery background tasks for Stitchfren.
"""

from app.core.celery_app import celery_app
from app.drafting.engine import generate_pattern
from app.nesting.engine import nest_pieces, naive_layout_baseline
from app.exporters.dxf import export_to_dxf
from app.storage import r2
from app.svg_export import render_pattern_pieces_svg, render_nested_layout_svg
from app.models.schemas import PatternRequest
from app.db.database import SessionLocal
from app.db.models import Job
from app.services.llm_service import generate_cutting_sheet
import asyncio
import hashlib
import json
import os
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

        # 1. Generate pattern pieces
        pieces = generate_pattern(
            style=request.style.value,
            m=request.measurements,
            include_seam_allowance=request.include_seam_allowance,
            seam_allowance_cm=request.seam_allowance_cm,
        )

        piece_dicts = [{"label": p.label, "points": p.points} for p in pieces]
        piece_lookup = {p.label: p.points for p in pieces}

        # 2. Nesting
        nested = nest_pieces(piece_dicts, request.fabric_width_cm)
        naive = naive_layout_baseline(piece_dicts, request.fabric_width_cm)

        fabric_saved_cm = round(naive.fabric_length_used_cm - nested.fabric_length_used_cm, 1)
        fabric_saved_pct = round(100 * fabric_saved_cm / naive.fabric_length_used_cm, 1) if naive.fabric_length_used_cm > 0 else 0

        # 3. Generate SVGs
        pattern_svg = render_pattern_pieces_svg(pieces)
        layout_svg = render_nested_layout_svg(
            piece_lookup, [p.model_dump() for p in nested.placements],
            request.fabric_width_cm, nested.fabric_length_used_cm
        )

        # 4. Generate DXF
        dxf_filename = f"/tmp/stitchfren_{hashlib.md5(json.dumps(request_data, sort_keys=True).encode()).hexdigest()[:10]}.dxf"
        dxf_url = None
        try:
            export_to_dxf(pieces, request.fabric_width_cm, nested.fabric_length_used_cm, dxf_filename)
            if r2.is_configured():
                # Required on Railway: the web service and this worker are
                # separate instances/filesystems, so local /tmp files
                # written here are invisible to the web service.
                dxf_url = r2.upload_dxf(dxf_filename)
                os.remove(dxf_filename)
            else:
                # Local/VPS fallback only - works when the API and worker
                # share a filesystem (e.g. docker-compose on one host).
                dxf_url = f"/download/dxf/{dxf_filename.split('/')[-1]}"
        except Exception:
            dxf_url = None

        result_hash = hashlib.sha256(json.dumps(request_data, sort_keys=True).encode()).hexdigest()[:16]

        # Cutting sheet (rule-based; LLM enhancement happens separately via /api/parse-text)
        cutting_sheet = asyncio.run(generate_cutting_sheet(
            request, nested, naive, fabric_saved_cm, fabric_saved_pct
        ))

        warnings = []
        if request.allow_90_rotation:
            warnings.append("90° rotation enabled — verify grain compatibility with your fabric.")

        result = {
            "ok": True,
            "pattern_svg": pattern_svg,
            "layout_svg": layout_svg,
            "nested": nested.model_dump(),
            "naive": naive.model_dump(),
            "fabric_saved_cm": fabric_saved_cm,
            "fabric_saved_pct": fabric_saved_pct,
            "dxf_url": dxf_url,
            "result_hash": result_hash,
            "cutting_sheet": cutting_sheet,
            "warnings": warnings,
        }

        # Update job with results
        job.result_data = result
        job.status = "completed"
        job.fabric_saved_cm = fabric_saved_cm
        job.fabric_saved_pct = fabric_saved_pct
        job.result_hash = result_hash
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
