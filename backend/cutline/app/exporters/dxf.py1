"""
DXF export for Stitchfren pattern pieces.

Writes one closed LWPOLYLINE per piece (its cut line, seam allowance already
baked in by drafting/engine.py if requested) plus a text label, all in cm,
so the file opens at real-world scale in a CAD/cutting-plotter tool.
"""

from __future__ import annotations

from typing import List

import ezdxf
from ezdxf.enums import TextEntityAlignment

from ..drafting.engine import PatternPiece

OUTLINE_LAYER = "PIECE_OUTLINE"
LABEL_LAYER = "PIECE_LABEL"


def export_to_dxf(
    pieces: List[PatternPiece],
    fabric_width_cm: float,
    fabric_length_cm: float,
    filename: str,
) -> str:
    """Writes `pieces` to `filename` as a DXF (R2010) and returns the path."""
    doc = ezdxf.new("R2010", setup=True)
    doc.header["$INSUNITS"] = ezdxf.units.CM

    doc.layers.add(OUTLINE_LAYER, color=5)   # blue
    doc.layers.add(LABEL_LAYER, color=3)     # green

    msp = doc.modelspace()

    for piece in pieces:
        points = list(piece.points)
        if len(points) < 3:
            continue

        msp.add_lwpolyline(
            points,
            dxfattribs={"layer": OUTLINE_LAYER, "closed": True},
        )

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        cx = (min(xs) + max(xs)) / 2
        top_y = max(ys)

        text = msp.add_text(
            piece.label,
            dxfattribs={"layer": LABEL_LAYER, "height": 1.5},
        )
        text.set_placement(
            (cx, top_y + 1.0),
            align=TextEntityAlignment.MIDDLE_CENTER,
        )

    # Reference border showing the fabric roll extent, for visual sanity
    # when checking a layout DXF rather than a single-piece one.
    if fabric_width_cm and fabric_length_cm:
        msp.add_lwpolyline(
            [
                (0, 0),
                (fabric_width_cm, 0),
                (fabric_width_cm, fabric_length_cm),
                (0, fabric_length_cm),
            ],
            dxfattribs={"layer": OUTLINE_LAYER, "closed": True, "linetype": "DASHED"},
        )

    doc.saveas(filename)
    return filename
