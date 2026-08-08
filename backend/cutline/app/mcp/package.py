"""
Bundles a single paid job's deliverables - DXF, both SVGs, and a generated
PDF spec sheet - into one ZIP, and generates that PDF.

Why this exists: app/mcp/job.py used to hand a buyer three separate hosted
links (download_url, pattern_svg_url, layout_svg_url) plus a raw JSON
cutting_sheet dict. That's fine for a client that renders the tool result
richly, but the actual delivery surface for an OKX A2MCP buyer is a
terminal/agent conversation - three loose links and an implicit "read the
JSON to get your cutting instructions" isn't a professional deliverable
there. This module turns the same underlying data into:

  1. A cover-page + tables PDF (build_spec_sheet_pdf) - the human-readable
     write-up: order details, measurements actually used, piece list,
     fabric-savings summary, cutting notes, and the LLM narrative if one
     was generated. This is the thing a buyer opens first.
  2. One ZIP (build_deliverable_zip) containing that PDF alongside the
     real DXF, pattern SVG, and layout SVG files - one download, not four.

job.py calls both and uploads the ZIP the same way it already uploads the
DXF (via app.storage.r2), so this inherits the existing R2-configured /
local-/tmp-fallback split rather than reimplementing it.
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    HRFlowable,
)

BRAND_INK = colors.HexColor("#1F2A37")
BRAND_ACCENT = colors.HexColor("#C9384A")
BRAND_MUTED = colors.HexColor("#6B7280")
BRAND_RULE = colors.HexColor("#E5E7EB")


def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle(
        name="StitchTitle", fontName="Helvetica-Bold", fontSize=22,
        leading=27, textColor=BRAND_INK, spaceAfter=4,
    ))
    ss.add(ParagraphStyle(
        name="StitchSubtitle", fontName="Helvetica", fontSize=10.5,
        leading=13, textColor=BRAND_ACCENT, spaceAfter=14,
    ))
    ss.add(ParagraphStyle(
        name="StitchTableHeader", fontName="Helvetica-Bold", fontSize=9.5,
        textColor=colors.white,
    ))
    ss.add(ParagraphStyle(
        name="StitchH2", fontName="Helvetica-Bold", fontSize=12.5,
        textColor=BRAND_INK, spaceBefore=16, spaceAfter=6,
    ))
    ss.add(ParagraphStyle(
        name="StitchBody", fontName="Helvetica", fontSize=9.5,
        textColor=BRAND_INK, leading=14,
    ))
    ss.add(ParagraphStyle(
        name="StitchMuted", fontName="Helvetica", fontSize=8.5,
        textColor=BRAND_MUTED, leading=12,
    ))
    return ss


def _kv_table(rows: list[tuple[str, str]]) -> Table:
    t = Table(
        [[Paragraph(f"<b>{k}</b>", ParagraphStyle(
            "k", fontName="Helvetica-Bold", fontSize=9, textColor=BRAND_MUTED)),
          Paragraph(str(v), ParagraphStyle(
              "v", fontName="Helvetica", fontSize=9.5, textColor=BRAND_INK))]
         for k, v in rows],
        colWidths=[4.6 * cm, 11.0 * cm],
    )
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, BRAND_RULE),
    ]))
    return t


def build_spec_sheet_pdf(
    request_data: Dict[str, Any],
    result: Dict[str, Any],
) -> bytes:
    """
    Renders the professional write-up: cover section, order + measurement
    summary, piece list, fabric savings, cutting notes/narrative, and a
    verification footer (result_hash + generation timestamp). Pure
    read-only rendering of data job.py already computed - no new business
    logic, no re-derivation of numbers.

    Returns raw PDF bytes (kept in memory - the caller decides whether/
    where to write it to disk before zipping).
    """
    ss = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=letter,
        leftMargin=2.2 * cm, rightMargin=2.2 * cm,
        topMargin=2.0 * cm, bottomMargin=2.0 * cm,
        title="Stitchfren Cutting Sheet",
    )

    sheet = result.get("cutting_sheet") or {}
    style = sheet.get("style") or request_data.get("style", "")
    quantity = sheet.get("quantity") or request_data.get("quantity", 1)
    measurements = request_data.get("measurements") or {}
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    story = []

    story.append(Paragraph("stitch<font color='#C9384A'>fren</font>", ss["StitchTitle"]))
    story.append(Paragraph("Cut-Ready Pattern &amp; Cutting Sheet", ss["StitchSubtitle"]))
    story.append(HRFlowable(width="100%", thickness=1, color=BRAND_RULE, spaceAfter=12))

    story.append(Paragraph("Order Summary", ss["StitchH2"]))
    story.append(_kv_table([
        ("Style", str(style).replace("_", " ").title()),
        ("Quantity", f"{quantity} garment{'s' if quantity != 1 else ''}"),
        ("Fabric width", f"{request_data.get('fabric_width_cm', '—')} cm"),
        ("Seam allowance", (
            f"{request_data.get('seam_allowance_cm', 1.0)} cm"
            if request_data.get("include_seam_allowance", True) else
            "Not included — add before cutting"
        )),
        ("Generated", generated_at),
        ("Verification hash", result.get("result_hash", "—")),
    ]))

    meas_rows = []
    meas_labels = [
        ("bust_or_chest", "Bust / chest"), ("waist", "Waist"), ("hip", "Hip"),
        ("shoulder_width", "Shoulder width"), ("back_length", "Back length"),
        ("ease", "Ease"), ("sleeve_length", "Sleeve length"),
        ("shirt_length", "Shirt length"), ("skirt_length", "Skirt/dress length"),
        ("rise", "Rise"), ("trouser_length", "Trouser length"),
    ]
    for key, label in meas_labels:
        val = measurements.get(key)
        if val is not None:
            meas_rows.append((label, f"{val} cm"))
    if meas_rows:
        story.append(Paragraph("Measurements Used", ss["StitchH2"]))
        story.append(_kv_table(meas_rows))

    story.append(Paragraph("Fabric &amp; Nesting", ss["StitchH2"]))
    story.append(_kv_table([
        ("Fabric length needed", f"{sheet.get('fabric_length_needed_cm', '—')} cm"),
        ("Naive layout length", f"{sheet.get('naive_fabric_length_cm', '—')} cm"),
        ("Fabric saved", (
            f"{result.get('fabric_saved_cm', 0)} cm "
            f"({result.get('fabric_saved_pct', 0)}% less than a naive layout)"
        )),
    ]))

    pieces = sheet.get("pieces") or []
    if pieces:
        story.append(Paragraph("Pieces to Cut", ss["StitchH2"]))
        rows = [[Paragraph("#", ss["StitchTableHeader"]), Paragraph("Piece", ss["StitchTableHeader"])]]
        for i, label in enumerate(pieces, 1):
            rows.append([Paragraph(str(i), ss["StitchBody"]), Paragraph(str(label), ss["StitchBody"])])
        piece_table = Table(rows, colWidths=[1.2 * cm, 14.4 * cm])
        piece_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), BRAND_INK),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, BRAND_RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(piece_table)

    narrative = sheet.get("narrative")
    notes = sheet.get("notes") or []
    if narrative or notes:
        story.append(Paragraph("Cutting Instructions", ss["StitchH2"]))
        if narrative:
            story.append(Paragraph(narrative, ss["StitchBody"]))
            story.append(Spacer(1, 6))
        for n in notes:
            story.append(Paragraph(f"&bull; {n}", ss["StitchBody"]))

    warnings = result.get("warnings") or []
    if warnings:
        story.append(Paragraph("Warnings", ss["StitchH2"]))
        for w in warnings:
            story.append(Paragraph(f"&bull; {w}", ss["StitchBody"]))

    story.append(Spacer(1, 22))
    story.append(HRFlowable(width="100%", thickness=0.75, color=BRAND_RULE, spaceAfter=8))
    story.append(Paragraph(
        "This sheet accompanies the enclosed DXF and SVG files. Verify the "
        "verification hash above matches your order confirmation before "
        "cutting fabric. Generated by Stitchfren.",
        ss["StitchMuted"],
    ))

    doc.build(story)
    return buf.getvalue()


def build_deliverable_zip(
    zip_local_path: str,
    *,
    pdf_bytes: bytes,
    dxf_local_path: Optional[str],
    pattern_svg: str,
    layout_svg: str,
    base_name: str = "stitchfren_pattern",
) -> str:
    """
    Writes a ZIP to zip_local_path containing:
      - {base_name}_cutting_sheet.pdf
      - {base_name}.dxf            (only if dxf_local_path exists on disk -
                                     DXF export can fail independently, see
                                     job.py's try/except around export_to_dxf)
      - {base_name}_pattern.svg    (single reference garment diagram)
      - {base_name}_layout.svg     (nested cutting layout)

    Returns zip_local_path unchanged, for chaining into the caller's
    upload step.
    """
    with zipfile.ZipFile(zip_local_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base_name}_cutting_sheet.pdf", pdf_bytes)
        if dxf_local_path and os.path.exists(dxf_local_path):
            zf.write(dxf_local_path, arcname=f"{base_name}.dxf")
        zf.writestr(f"{base_name}_pattern.svg", pattern_svg)
        zf.writestr(f"{base_name}_layout.svg", layout_svg)
    return zip_local_path
