"""
DXF export for Stitchfren pattern pieces.

Writes the NESTED layout - the same one shown in the layout SVG and used
for the fabric-savings numbers - to a DXF (R2010), in cm, with four things
a professional pattern-cutting file is expected to have:

  1. The cut line (PIECE_OUTLINE layer) - the outer, seam-allowance-
     included boundary.
  2. The stitch/sew line (STITCH_LINE layer) - the original pre-allowance
     outline, inset from the cut line, when a seam allowance was applied.
  3. A grainline arrow per piece (GRAINLINE layer), from PatternPiece's
     grain_angle - every current style is 0.0 (straight/lengthwise grain),
     but the geometry here is general, not hardcoded vertical.
  4. The fabric roll extent (FABRIC_BOUNDARY layer) - a reference only,
     kept off the cut-line layer so it can't be mistaken for a piece.

All three per-piece geometries (cut, stitch, grainline) are placed with
the SAME transform, derived once from the cut line's own centroid/bbox -
see _piece_transform()'s docstring for why that matters: a naive
independent transform of each geometry would NOT necessarily keep the
stitch line correctly inset relative to the cut line after a 180-deg
rotation, especially for concave/asymmetric pieces (dart notches).
"""

from __future__ import annotations

import math
from typing import Callable, Dict, List, Optional, Tuple

import ezdxf
from ezdxf.enums import TextEntityAlignment

from ..svg_export import _short_label, _job_caption

OUTLINE_LAYER = "PIECE_OUTLINE"
STITCH_LAYER = "STITCH_LINE"
GRAIN_LAYER = "GRAINLINE"
LABEL_LAYER = "PIECE_LABEL"
BOUNDARY_LAYER = "FABRIC_BOUNDARY"

Point = Tuple[float, float]


def _piece_transform(
    cut_points: List[Point],
    placement: Dict,
    fabric_length_cm: float,
) -> Callable[[List[Point]], List[Point]]:
    """
    Returns a function mapping LOCAL (un-rotated, un-translated) points to
    their final placed position on the fabric sheet - reused identically
    for the cut line, the stitch line, and the grainline endpoints so all
    three stay correctly aligned with each other.

    Rotation is a true 180-degree point reflection about the CUT line's
    own centroid (matching app/nesting/engine.py's flip_points exactly,
    which is what nest_pieces actually used when it decided this
    placement). Reflecting the stitch line about its OWN centroid instead
    would silently misalign it from the cut line for any piece where a
    uniform seam-allowance offset doesn't happen to share the exact same
    centroid as the original - true for a plain rectangle, not guaranteed
    for a piece with dart notches (mitred offsets grow concave corners
    asymmetrically). Sharing one pivot removes that risk entirely.
    """
    rotated = bool(placement.get("rotated_180"))

    if rotated:
        cx = sum(p[0] for p in cut_points) / len(cut_points)
        cy = sum(p[1] for p in cut_points) / len(cut_points)

        def rotate(points: List[Point]) -> List[Point]:
            return [(2 * cx - x, 2 * cy - y) for x, y in points]
    else:
        def rotate(points: List[Point]) -> List[Point]:
            return points

    rotated_cut = rotate(cut_points)
    min_x = min(p[0] for p in rotated_cut)
    min_y = min(p[1] for p in rotated_cut)
    ox = placement["x_offset_cm"]
    oy_top = fabric_length_cm - placement["y_offset_cm"]

    def transform(points: List[Point]) -> List[Point]:
        pts = rotate(points)
        return [(ox - min_x + x, oy_top + min_y - y) for x, y in pts]

    return transform


def _grainline_local(cut_points: List[Point], grain_angle_deg: float, length_fraction: float = 0.7) -> List[Point]:
    """
    A straight line through the piece's centroid, in the piece's own local
    (un-rotated) frame, at grain_angle_deg from vertical (0deg = straight/
    lengthwise grain, running parallel to the piece's own up-down axis -
    matches the fixed vertical arrow app/svg_export.py's flat-pieces view
    already draws for every current style, all of which use grain_angle=0).
    Length is a fraction of the piece's larger bounding-box dimension, so
    it scales sensibly across very different piece sizes rather than using
    a fixed length that's too long for a sleeve or too short for a skirt
    panel.
    """
    cx = sum(p[0] for p in cut_points) / len(cut_points)
    cy = sum(p[1] for p in cut_points) / len(cut_points)
    xs = [p[0] for p in cut_points]
    ys = [p[1] for p in cut_points]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    half_len = span * length_fraction / 2

    theta = math.radians(grain_angle_deg)
    dx, dy = math.sin(theta), math.cos(theta)
    return [(cx - dx * half_len, cy - dy * half_len), (cx + dx * half_len, cy + dy * half_len)]


def _add_arrowhead(msp, tip: Point, direction: Point, layer: str, size: float = 1.2) -> None:
    """Small open-V arrowhead at `tip`, pointing along `direction` (unit
    vector). Drawn as two short lines rather than a filled block/insert -
    keeps the DXF free of block-definition boilerplate for something this
    simple, and every CAD/cutter tool renders plain LINE entities."""
    dx, dy = direction
    # perpendicular vector for the two barbs
    px, py = -dy, dx
    back = (tip[0] - dx * size, tip[1] - dy * size)
    barb1 = (back[0] + px * size * 0.5, back[1] + py * size * 0.5)
    barb2 = (back[0] - px * size * 0.5, back[1] - py * size * 0.5)
    msp.add_line(tip, barb1, dxfattribs={"layer": layer})
    msp.add_line(tip, barb2, dxfattribs={"layer": layer})


def export_to_dxf(
    piece_lookup: Dict[str, List[Point]],
    placements: List[Dict],
    fabric_width_cm: float,
    fabric_length_cm: float,
    filename: str,
    stitch_lookup: Optional[Dict[str, List[Point]]] = None,
    grain_lookup: Optional[Dict[str, float]] = None,
) -> str:
    """
    Writes the NESTED layout to `filename` as a DXF (R2010) and returns the
    path.

    piece_lookup / placements: the same shapes render_nested_layout_svg
    takes - piece_lookup maps a piece's label to its cut-line points,
    placements is nested.placements (each entry carrying label,
    x_offset_cm, y_offset_cm, rotated_180).
    stitch_lookup: label -> pre-seam-allowance points (PatternPiece.
    stitch_points). Labels with no seam allowance applied are simply
    absent - no separate stitch line to draw in that case, since the cut
    line already IS the stitch line.
    grain_lookup: label -> grain_angle in degrees, 0 = straight/lengthwise
    grain (every current style). Missing labels default to 0.0.

    THE ORIGINAL BUG THIS FIXES: earlier versions took the raw drafted
    pieces directly and drew each one's points as-is. Every drafting
    function in app/drafting/engine.py starts its piece at local (0, 0), so
    every piece in a job landed stacked on top of each other at the origin
    instead of laid out on the fabric - the exported "cut-ready DXF" was,
    for any multi-piece job, an unusable pile of overlapping outlines.
    Pulling piece_lookup + the nested placements instead (same data
    render_nested_layout_svg already uses correctly) fixes this by
    construction.
    """
    stitch_lookup = stitch_lookup or {}
    grain_lookup = grain_lookup or {}

    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = ezdxf.units.CM
    doc.header["$MEASUREMENT"] = 1  # metric - some importers check this separately from $INSUNITS

    doc.layers.add(OUTLINE_LAYER, color=5)    # blue  - cut line
    doc.layers.add(STITCH_LAYER, color=1)     # red   - sew line
    doc.layers.add(GRAIN_LAYER, color=2)      # yellow - grainline arrows
    doc.layers.add(LABEL_LAYER, color=3)      # green - piece labels
    doc.layers.add(BOUNDARY_LAYER, color=8)   # grey  - fabric roll extent, NOT a cut line

    msp = doc.modelspace()

    caption = ""
    for placement in placements:
        label = placement.get("label", "")
        cut_points = piece_lookup.get(label)
        if not cut_points or len(cut_points) < 3:
            continue

        transform = _piece_transform(cut_points, placement, fabric_length_cm)

        placed_cut = transform(cut_points)
        msp.add_lwpolyline(placed_cut, dxfattribs={"layer": OUTLINE_LAYER, "closed": True})

        stitch_points = stitch_lookup.get(label)
        if stitch_points and len(stitch_points) >= 3:
            placed_stitch = transform(stitch_points)
            msp.add_lwpolyline(
                placed_stitch,
                dxfattribs={"layer": STITCH_LAYER, "closed": True, "linetype": "DASHED"},
            )

        grain_angle = grain_lookup.get(label, 0.0)
        g_start_local, g_end_local = _grainline_local(cut_points, grain_angle)
        placed_grain = transform([g_start_local, g_end_local])
        g_start, g_end = placed_grain[0], placed_grain[1]
        msp.add_line(g_start, g_end, dxfattribs={"layer": GRAIN_LAYER})
        gdx = g_end[0] - g_start[0]
        gdy = g_end[1] - g_start[1]
        glen = math.hypot(gdx, gdy) or 1.0
        gdir = (gdx / glen, gdy / glen)
        _add_arrowhead(msp, g_end, gdir, GRAIN_LAYER)
        _add_arrowhead(msp, g_start, (-gdir[0], -gdir[1]), GRAIN_LAYER)

        if not caption:
            caption = _job_caption(label)

        xs = [p[0] for p in placed_cut]
        ys = [p[1] for p in placed_cut]
        cx = (min(xs) + max(xs)) / 2
        top_y = max(ys)

        text = msp.add_text(
            _short_label(label),
            dxfattribs={"layer": LABEL_LAYER, "height": 1.5},
        )
        text.set_placement((cx, top_y + 1.0), align=TextEntityAlignment.MIDDLE_CENTER)

    if fabric_width_cm and fabric_length_cm:
        msp.add_lwpolyline(
            [
                (0, 0),
                (fabric_width_cm, 0),
                (fabric_width_cm, fabric_length_cm),
                (0, fabric_length_cm),
            ],
            dxfattribs={"layer": BOUNDARY_LAYER, "closed": True, "linetype": "DASHED"},
        )

    if caption:
        job_text = msp.add_text(
            caption.strip("() ").replace(") (", " \u2014 "),
            dxfattribs={"layer": LABEL_LAYER, "height": 1.2},
        )
        job_text.set_placement(
            (fabric_width_cm / 2 if fabric_width_cm else 0, (fabric_length_cm or 0) + 3),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    doc.saveas(filename)
    return filename
