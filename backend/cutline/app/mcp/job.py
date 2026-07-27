"""
Shared pattern-drafting-and-nesting job logic.

This is the same compute pipeline that used to live only inside
app/workers/tasks.py's generate_pattern_task. It's pulled out here, verbatim
in behavior, so two very different callers can share one implementation
instead of drifting apart:

  - app/workers/tasks.py (Celery task, old REST API): still queues this via
    Celery and polls, still writes a Job row to Postgres. Unchanged for
    existing frontend/API consumers.
  - app/mcp/server.py (new MCP tool): calls this directly, in-process,
    awaits the result, and returns it inline in a single tools/call response
    - no polling, because an OKX buyer's agent calls the tool once and
      expects one answer back (per the A2MCP requirement that the endpoint
      be self-contained).

Nothing in app/drafting, app/nesting, app/exporters, or app/svg_export
changed - this module only re-arranges how their outputs get assembled,
so the actual drafting/nesting math is identical for both callers.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict

from app.drafting.engine import generate_pattern
from app.nesting.engine import nest_pieces, naive_layout_baseline
from app.exporters.dxf import export_to_dxf
from app.storage import r2
from app.svg_export import render_pattern_pieces_svg, render_nested_layout_svg
from app.models.schemas import PatternRequest
from app.services.llm_service import generate_cutting_sheet


async def run_pattern_job(request: PatternRequest, request_data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """
    Runs draft + nest + DXF export + cutting sheet to completion and returns
    the full result dict. Async because generate_cutting_sheet's optional
    LLM call is async - callers in a sync context (Celery) wrap this in
    asyncio.run(), callers already in an async context (the MCP tool,
    FastAPI) just await it directly.

    request_data: the raw dict form of the request, used only for the
    result_hash/DXF-filename hash below. If omitted, request.model_dump()
    is used - the two are only ever different when a caller wants the hash
    to reflect exactly the wire payload it received (Celery's case, where
    request_data is the untouched dict handed to generate_pattern_task).
    """
    if request_data is None:
        request_data = request.model_dump()

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
    fabric_saved_pct = (
        round(100 * fabric_saved_cm / naive.fabric_length_used_cm, 1)
        if naive.fabric_length_used_cm > 0 else 0
    )

    # 3. SVGs
    pattern_svg = render_pattern_pieces_svg(pieces)
    layout_svg = render_nested_layout_svg(
        piece_lookup, [p.model_dump() for p in nested.placements],
        request.fabric_width_cm, nested.fabric_length_used_cm
    )

    # 4. DXF
    dxf_filename = f"/tmp/stitchfren_{hashlib.md5(json.dumps(request_data, sort_keys=True).encode()).hexdigest()[:10]}.dxf"
    dxf_url = None
    try:
        export_to_dxf(pieces, request.fabric_width_cm, nested.fabric_length_used_cm, dxf_filename)
        if r2.is_configured():
            dxf_url = r2.upload_dxf(dxf_filename)
            os.remove(dxf_filename)
        else:
            dxf_url = f"/download/dxf/{dxf_filename.split('/')[-1]}"
    except Exception:
        dxf_url = None

    # 4b. SVGs - same idea as the DXF above: upload to R2 so the response
    # can carry real download links, not just inline markup. pattern_svg/
    # layout_svg keep returning the raw markup too (existing consumers,
    # e.g. inline demo previews, still get that unchanged) - these _url
    # fields are additive.
    file_hash = hashlib.md5(json.dumps(request_data, sort_keys=True).encode()).hexdigest()[:10]
    pattern_svg_url = None
    layout_svg_url = None
    if r2.is_configured():
        for svg_content, kind, target in (
            (pattern_svg, "pattern", "pattern_svg_url"),
            (layout_svg, "layout", "layout_svg_url"),
        ):
            svg_filename = f"/tmp/stitchfren_{kind}_{file_hash}.svg"
            try:
                # Explicit UTF-8: cutting-sheet text (and this SVG content)
                # can carry non-ASCII characters (degree signs, dashes, an
                # LLM narrative). Without pinning encoding, a server whose
                # default locale isn't UTF-8 raises UnicodeEncodeError here,
                # which the except below would otherwise swallow silently -
                # leaving pattern_svg_url/layout_svg_url as None with no
                # visible error.
                with open(svg_filename, "w", encoding="utf-8") as f:
                    f.write(svg_content)
                url = r2.upload_svg(svg_filename)
                os.remove(svg_filename)
                if target == "pattern_svg_url":
                    pattern_svg_url = url
                else:
                    layout_svg_url = url
            except Exception:
                pass

    result_hash = hashlib.sha256(json.dumps(request_data, sort_keys=True).encode()).hexdigest()[:16]

    # 5. Cutting sheet (rule-based, + LLM narrative if LLM_API_KEY is set)
    cutting_sheet = await generate_cutting_sheet(
        request, nested, naive, fabric_saved_cm, fabric_saved_pct
    )

    warnings = []
    if request.allow_90_rotation:
        warnings.append("90\u00b0 rotation enabled \u2014 verify grain compatibility with your fabric.")

    return {
        "ok": True,
        "pattern_svg": pattern_svg,
        "layout_svg": layout_svg,
        "pattern_svg_url": pattern_svg_url,
        "layout_svg_url": layout_svg_url,
        "nested": nested.model_dump(),
        "naive": naive.model_dump(),
        "fabric_saved_cm": fabric_saved_cm,
        "fabric_saved_pct": fabric_saved_pct,
        "dxf_url": dxf_url,
        "result_hash": result_hash,
        "cutting_sheet": cutting_sheet,
        "warnings": warnings,
    }


def fallback_direction(result: Dict[str, Any]) -> str:
    """
    Plain-language fallback summary when the cutting sheet has no LLM
    narrative. Ported from backend/mcp-gateway/server.js's fallbackDirection
    so the new Python MCP tool gives the same kind of front-and-center
    "message" an agent gets, without depending on the Node gateway.
    """
    sheet = result.get("cutting_sheet") or {}
    pieces = sheet.get("pieces")
    piece_count = len(pieces) if isinstance(pieces, list) else None

    parts = []
    if sheet.get("style"):
        parts.append(f"Your {str(sheet['style']).replace('_', ' ')} pattern is ready.")
    if piece_count:
        parts.append(f"Cut {piece_count} piece{'s' if piece_count != 1 else ''}.")
    if sheet.get("fabric_length_needed_cm"):
        parts.append(f"Uses {sheet['fabric_length_needed_cm']}cm of fabric.")
    if result.get("fabric_saved_cm", 0) and result["fabric_saved_cm"] > 0:
        parts.append(
            f"That's {result['fabric_saved_cm']}cm ({result.get('fabric_saved_pct')}%) "
            "less than a naive layout."
        )
    notes = sheet.get("notes")
    if isinstance(notes, list) and notes:
        parts.append(" ".join(notes))

    return " ".join(parts) or "Your pattern and nested cutting layout are ready."
