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
import re
from typing import Any, Dict

from app.drafting.engine import generate_pattern
from app.nesting.engine import nest_pieces, naive_layout_baseline
from app.exporters.dxf import export_to_dxf
from app.storage import r2
from app.svg_export import render_pattern_pieces_svg, render_nested_layout_svg
from app.models.schemas import PatternRequest
from app.services.llm_service import generate_cutting_sheet
from app.mcp.package import build_spec_sheet_pdf, build_deliverable_zip


async def run_pattern_job(
    request: PatternRequest,
    request_data: Dict[str, Any] | None = None,
    skip_llm: bool = False,
    skip_package: bool = False,
) -> Dict[str, Any]:
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

    skip_llm: passed straight to generate_cutting_sheet. Used by the free
    draft_and_nest_pattern_preview MCP tool to skip the one real per-call
    dollar cost (the LLM narrative) in an otherwise-free call.

    skip_package: skips step 6 below (PDF spec sheet + ZIP bundle) entirely
    - package_url is None and no zip/PDF work happens. Used by the same
    free preview tool: it already discards download_url and never exposes
    package_url, so building the bundle for it would just burn CPU and (if
    R2 is configured) storage for a file nobody gets a link to.
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

    # piece_lookup/stitch_lookup/grain_lookup are keyed by label and used
    # later to render/export the NESTED layout, which needs one entry per
    # placed piece - including every numbered copy below when quantity > 1.
    # pattern_svg (the single reference garment diagram) uses `pieces`
    # directly and is unaffected by quantity.
    piece_lookup = {p.label: p.points for p in pieces}
    stitch_lookup = {p.label: p.stitch_points for p in pieces if p.stitch_points}
    grain_lookup = {p.label: p.grain_angle for p in pieces}

    quantity = max(1, request.quantity)
    piece_dicts = []
    for copy_num in range(1, quantity + 1):
        for p in pieces:
            label = p.label if quantity == 1 else f"{p.label} #{copy_num}"
            piece_dicts.append({"label": label, "points": p.points})
            if quantity > 1:
                piece_lookup[label] = p.points
                if p.stitch_points:
                    stitch_lookup[label] = p.stitch_points
                grain_lookup[label] = p.grain_angle

    # 2. Nesting
    nested = nest_pieces(piece_dicts, request.fabric_width_cm, margin_cm=1.5)
    naive = naive_layout_baseline(piece_dicts, request.fabric_width_cm, gap_cm=1.5)

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

    # 4. DXF - built from the NESTED layout (piece_lookup + placements),
    # same as layout_svg below, not the raw `pieces` list. See
    # app/exporters/dxf.py's module docstring: passing raw pieces here
    # used to draw every piece stacked on top of each other at local
    # (0,0) instead of laid out on the fabric.
    dxf_filename = f"/tmp/stitchfren_{hashlib.md5(json.dumps(request_data, sort_keys=True).encode()).hexdigest()[:10]}.dxf"
    dxf_url = None
    dxf_export_ok = False
    try:
        export_to_dxf(
            piece_lookup, [p.model_dump() for p in nested.placements],
            request.fabric_width_cm, nested.fabric_length_used_cm, dxf_filename,
            stitch_lookup=stitch_lookup, grain_lookup=grain_lookup,
        )
        dxf_export_ok = True
        if r2.is_configured():
            dxf_url = r2.upload_dxf(dxf_filename)
            # NOTE: local dxf_filename is deliberately kept on disk here
            # (not removed like before) - the package step below (6.) still
            # needs it to build the ZIP. It's removed in the cleanup step
            # after the ZIP is built, regardless of which branch ran here.
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

    # 5. Cutting sheet (rule-based, + LLM narrative if LLM_API_KEY is set and skip_llm is False)
    cutting_sheet = await generate_cutting_sheet(
        request, nested, naive, fabric_saved_cm, fabric_saved_pct, skip_llm=skip_llm
    )

    warnings = []
    if request.allow_90_rotation:
        warnings.append("90\u00b0 rotation enabled \u2014 verify grain compatibility with your fabric.")

    result_so_far = {
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

    # 6. Package: bundle the PDF spec sheet + DXF + both SVGs into one ZIP,
    # so a terminal/agent buyer gets a single professional download instead
    # of three loose links plus a JSON blob to read. Built from data already
    # computed above - no re-derivation, pure assembly. Never lets a
    # packaging failure fail an otherwise-successful job: on any exception
    # package_url stays None and the individual dxf_url/svg URLs above are
    # still returned as a fallback.
    package_url = None
    zip_filename = f"/tmp/stitchfren_package_{file_hash}.zip"
    if not skip_package:
        try:
            pdf_bytes = build_spec_sheet_pdf(request_data, result_so_far)
            build_deliverable_zip(
                zip_filename,
                pdf_bytes=pdf_bytes,
                dxf_local_path=dxf_filename if dxf_export_ok else None,
                pattern_svg=pattern_svg,
                layout_svg=layout_svg,
                base_name=f"stitchfren_{request.style.value}",
            )
            if r2.is_configured():
                package_url = r2.upload_file(zip_filename, "packages", "application/zip")
            else:
                package_url = f"/download/package/{zip_filename.split('/')[-1]}"
        except Exception:
            package_url = None

    # Clean up every local temp file now that the ZIP (if it built) has
    # already read whatever it needed from them. Only actually removes
    # files that exist - safe to call even when skip_package is True (the
    # dxf_url-only fallback branch still needs to skip removal for that
    # file) or when packaging failed above.
    for path, keep_for_local_fallback in (
        (dxf_filename, dxf_url == f"/download/dxf/{dxf_filename.split('/')[-1]}"),
        (zip_filename, package_url == f"/download/package/{zip_filename.split('/')[-1]}"),
    ):
        if keep_for_local_fallback:
            continue  # local /download/* route below still needs this on disk
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    result_so_far["package_url"] = package_url
    return result_so_far


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
    quantity = sheet.get("quantity") or 1
    if sheet.get("style"):
        run_note = f" x{quantity}" if quantity > 1 else ""
        parts.append(f"Your {str(sheet['style']).replace('_', ' ')} pattern{run_note} is ready.")
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


def watermark_svg(svg: str, label: str = "PREVIEW \u2014 NOT TO SCALE") -> str:
    """
    Stamps a diagonal, semi-transparent watermark across an SVG string,
    used by the free draft_and_nest_pattern_preview MCP tool. Two reasons
    this exists instead of just omitting the DXF: (1) without it, a caller
    could print the preview SVG at 1:1 scale as a free workaround for the
    paid DXF; (2) it puts the upsell in the artifact itself, not just in a
    text field an agent might not surface to whoever it's working for.

    Purely a string insertion before the closing </svg> tag - doesn't touch
    render_pattern_pieces_svg/render_nested_layout_svg or anything else
    that builds the paid tool's output.
    """
    match = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    if not match:
        return svg  # unexpected shape - fail open rather than corrupt the SVG

    w, h = float(match.group(1)), float(match.group(2))
    cx, cy = w / 2, h / 2
    font_size = max(min(w, h) * 0.13, 18)

    watermark = (
        f'<text x="{cx:.0f}" y="{cy:.0f}" '
        f'transform="rotate(-30 {cx:.0f} {cy:.0f})" '
        f'text-anchor="middle" dominant-baseline="middle" '
        f'font-family="IBM Plex Mono, monospace" font-weight="700" '
        f'font-size="{font_size:.0f}" fill="#C9384A" fill-opacity="0.28" '
        f'stroke="none">{label}</text>'
    )
    return svg.replace("</svg>", watermark + "</svg>")
