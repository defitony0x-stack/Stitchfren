"""
Improved pattern drafting engine.

Implements standard block/sloper drafting with better structure than v1.
Supports seam allowance addition (simple offset).
All measurements in cm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple, Optional
import math

from ..models.schemas import Measurements as PydanticMeasurements  # reuse validation


Point = Tuple[float, float]


@dataclass
class PatternPiece:
    label: str
    points: List[Point]          # Closed polygon, (x, y) with y increasing downward (drafting convention)
    grain_angle: float = 0.0
    seam_pairs: List[Tuple[int, int]] = field(default_factory=list)  # indices of seams that match


def _default_shoulder(bust_or_chest: float) -> float:
    return bust_or_chest / 5 + 4.5


def _offset_polygon(points: List[Point], offset: float, is_outer: bool = True) -> List[Point]:
    """
    Simple parallel curve offset for seam allowance.
    For production use shapely or pyclipper for robust miter/round joins.
    This is a basic implementation sufficient for v1.1 straight/ gently curved pieces.
    """
    if offset <= 0:
        return points[:]

    n = len(points)
    new_points = []
    sign = 1 if is_outer else -1

    for i in range(n):
        p0 = points[(i - 1) % n]
        p1 = points[i]
        p2 = points[(i + 1) % n]

        # Compute outward normals
        dx1, dy1 = p1[0] - p0[0], p1[1] - p0[1]
        len1 = math.hypot(dx1, dy1) or 1e-9
        nx1, ny1 = -dy1 / len1, dx1 / len1   # left normal (assuming CCW? our points are mixed)

        dx2, dy2 = p2[0] - p1[0], p2[1] - p1[1]
        len2 = math.hypot(dx2, dy2) or 1e-9
        nx2, ny2 = -dy2 / len2, dx2 / len2

        # Average normal
        nx = (nx1 + nx2) / 2
        ny = (ny1 + ny2) / 2
        nlen = math.hypot(nx, ny) or 1e-9
        nx, ny = nx / nlen, ny / nlen

        new_x = p1[0] + sign * offset * nx
        new_y = p1[1] + sign * offset * ny
        new_points.append((new_x, new_y))

    return new_points


def add_seam_allowance(piece: PatternPiece, allowance: float) -> PatternPiece:
    """Returns a new piece with seam allowance added (outer offset)."""
    if allowance <= 0:
        return piece
    new_points = _offset_polygon(piece.points, allowance, is_outer=True)
    return PatternPiece(
        label=f"{piece.label} (SA {allowance}cm)",
        points=new_points,
        grain_angle=piece.grain_angle,
    )


# --- Drafting functions (improved from original) ---

def bodice_front(m: PydanticMeasurements) -> PatternPiece:
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    quarter_bust = (m.bust_or_chest + m.ease) / 4
    quarter_waist = (m.waist + m.ease) / 4
    nape_to_waist = m.back_length
    armhole_depth = m.bust_or_chest / 4 + 2.5
    dart_width = 2.5

    points = [
        (0.0, 0.0),                                           # CF neck
        (shoulder / 2, -1.5),
        (quarter_bust * 0.7, -armhole_depth * 0.35),          # armhole curve approx
        (quarter_bust, -armhole_depth * 0.55),
        (quarter_bust, -armhole_depth),
        (quarter_bust - dart_width / 2, -nape_to_waist + 2.5),
        (quarter_bust, -nape_to_waist),
        (quarter_waist, -nape_to_waist),
        (0.0, -nape_to_waist),
    ]
    return PatternPiece(label="Bodice Front", points=points, grain_angle=0.0)


def bodice_back(m: PydanticMeasurements) -> PatternPiece:
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    quarter_bust = (m.bust_or_chest + m.ease) / 4
    quarter_waist = (m.waist + m.ease) / 4
    nape_to_waist = m.back_length - 1.5
    armhole_depth = m.bust_or_chest / 4 + 2.0

    points = [
        (0.0, 0.0),
        (shoulder / 2, -1.0),
        (quarter_bust * 0.7, -armhole_depth * 0.35),
        (quarter_bust, -armhole_depth * 0.55),
        (quarter_bust, -armhole_depth),
        (quarter_bust - 1.5, -nape_to_waist + 2.5),
        (quarter_bust, -nape_to_waist),
        (quarter_waist, -nape_to_waist),
        (0.0, -nape_to_waist),
    ]
    return PatternPiece(label="Bodice Back", points=points, grain_angle=0.0)


def straight_skirt_front_or_back(m: PydanticMeasurements, is_front: bool) -> PatternPiece:
    quarter_waist = (m.waist + m.ease / 2) / 4
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4   # fallback if no hip
    length = m.skirt_length
    dart_width = 2.0 if is_front else 2.5
    hip_depth = 20.0

    points = [
        (0.0, 0.0),
        (quarter_waist, 0.0),
        (quarter_waist - dart_width / 2, -hip_depth * 0.4),
        (quarter_hip, -hip_depth),
        (quarter_hip, -length),
        (0.0, -length),
    ]
    label = "Skirt Front (Straight)" if is_front else "Skirt Back (Straight)"
    return PatternPiece(label=label, points=points, grain_angle=0.0)


def aline_skirt_front_or_back(m: PydanticMeasurements, is_front: bool, flare: float = 6.0) -> PatternPiece:
    base = straight_skirt_front_or_back(m, is_front)
    pts = base.points
    hem_hip_x, hem_hip_y = pts[3]
    hem_x, hem_y = pts[4]
    new_points = pts[:3] + [
        (hem_hip_x, hem_hip_y),
        (hem_x + flare, hem_y),
        (pts[5][0], pts[5][1]),
    ]
    label = "Skirt Front (A-Line)" if is_front else "Skirt Back (A-Line)"
    return PatternPiece(label=label, points=new_points, grain_angle=0.0)


def mens_shirt_front(m: PydanticMeasurements) -> PatternPiece:
    quarter_chest = (m.bust_or_chest + m.ease) / 4
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    armhole_depth = m.bust_or_chest / 4 + 3.0
    length = m.shirt_length

    points = [
        (0.0, 0.0),
        (shoulder / 2, -1.0),
        (quarter_chest * 0.85, -armhole_depth * 0.4),
        (quarter_chest, -armhole_depth),
        (quarter_chest, -length),
        (0.0, -length),
    ]
    return PatternPiece(label="Shirt Front", points=points, grain_angle=0.0)


def mens_shirt_back(m: PydanticMeasurements) -> PatternPiece:
    piece = mens_shirt_front(m)
    piece.label = "Shirt Back"
    return piece


def set_in_sleeve(m: PydanticMeasurements, length: float, label: str = "Sleeve") -> PatternPiece:
    """
    Generic set-in sleeve, cap width/height scaled off bust_or_chest so the
    same shape works for both the men's shirt sleeve and a women's bodice
    sleeve - only the label and target length differ per caller.
    """
    cap_height = m.bust_or_chest / 10 + 4
    width = m.bust_or_chest / 4 + 6

    points = [
        (0.0, 0.0),
        (width / 2, cap_height),
        (width, 0.0),
        (width * 0.8, -length),
        (width * 0.2, -length),
    ]
    return PatternPiece(label=label, points=points, grain_angle=0.0)


def mens_shirt_sleeve(m: PydanticMeasurements) -> PatternPiece:
    return set_in_sleeve(m, m.sleeve_length, label="Sleeve")


def mens_shirt_sleeve_short(m: PydanticMeasurements) -> PatternPiece:
    # Short sleeve ends roughly mid-bicep - a fixed fraction of the full
    # sleeve_length measurement rather than a separate input field, same
    # approach the rest of this v1 engine takes for derived lengths.
    short_length = m.sleeve_length * 0.35
    return set_in_sleeve(m, short_length, label="Short Sleeve")


def generate_pattern(
    style: str,
    m: PydanticMeasurements,
    include_seam_allowance: bool = True,
    seam_allowance_cm: float = 1.0,
) -> List[PatternPiece]:
    """
    style: 'bodice_aline' | 'bodice_straight' | 'bodice_aline_sleeved' |
           'bodice_top' | 'skirt_straight' | 'skirt_aline' | 'mens_shirt' |
           'mens_shirt_short_sleeve'
    Returns list of PatternPiece (with optional seam allowance applied).
    """
    if style == "bodice_straight":
        pieces = [
            bodice_front(m), bodice_back(m),
            straight_skirt_front_or_back(m, True),
            straight_skirt_front_or_back(m, False),
        ]
    elif style == "bodice_aline":
        pieces = [
            bodice_front(m), bodice_back(m),
            aline_skirt_front_or_back(m, True),
            aline_skirt_front_or_back(m, False),
        ]
    elif style == "bodice_aline_sleeved":
        pieces = [
            bodice_front(m), bodice_back(m),
            aline_skirt_front_or_back(m, True),
            aline_skirt_front_or_back(m, False),
            set_in_sleeve(m, m.sleeve_length, label="Sleeve"),
            set_in_sleeve(m, m.sleeve_length, label="Sleeve"),
        ]
    elif style == "bodice_top":
        pieces = [bodice_front(m), bodice_back(m)]
    elif style == "skirt_straight":
        pieces = [
            straight_skirt_front_or_back(m, True),
            straight_skirt_front_or_back(m, False),
        ]
    elif style == "skirt_aline":
        pieces = [
            aline_skirt_front_or_back(m, True),
            aline_skirt_front_or_back(m, False),
        ]
    elif style == "mens_shirt":
        pieces = [
            mens_shirt_front(m), mens_shirt_back(m),
            mens_shirt_sleeve(m), mens_shirt_sleeve(m),
        ]
    elif style == "mens_shirt_short_sleeve":
        pieces = [
            mens_shirt_front(m), mens_shirt_back(m),
            mens_shirt_sleeve_short(m), mens_shirt_sleeve_short(m),
        ]
    else:
        raise ValueError(f"Unsupported style: {style}")

    if include_seam_allowance and seam_allowance_cm > 0:
        pieces = [add_seam_allowance(p, seam_allowance_cm) for p in pieces]

    return pieces
