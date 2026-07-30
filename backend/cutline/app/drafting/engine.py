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

from shapely.geometry import Polygon
from shapely.geometry.polygon import orient

from ..models.schemas import Measurements as PydanticMeasurements  # reuse validation


Point = Tuple[float, float]


@dataclass
class PatternPiece:
    label: str
    points: List[Point]          # Closed polygon, (x, y) with y increasing downward (drafting convention)
    grain_angle: float = 0.0
    seam_pairs: List[Tuple[int, int]] = field(default_factory=list)  # indices of seams that match
    # The pre-seam-allowance outline, set by add_seam_allowance() below.
    # None when no seam allowance was applied (points IS the stitch line in
    # that case - nothing to distinguish). Used by app/exporters/dxf.py to
    # draw the actual sew line alongside the cut line, which a "cut-ready"
    # pattern file should have and previously didn't - only the cut line
    # (points) survived once add_seam_allowance overwrote it.
    stitch_points: Optional[List[Point]] = None


def _default_shoulder(bust_or_chest: float) -> float:
    return bust_or_chest / 5 + 4.5


def _offset_polygon(points: List[Point], offset: float, is_outer: bool = True) -> List[Point]:
    """
    Seam allowance offset via shapely's buffer(), verified against
    shapely's own docs (shapely.buffer / Polygon.buffer, mitre join
    style): buffer's positive/negative distance IS the Minkowski
    sum/difference of the polygon with a disc, computed with full
    geometric correctness at every vertex - including the concave dart
    notches these pieces have, which the old per-vertex normal-averaging
    implementation could self-intersect on (its own comment admitted
    "basic implementation... sufficient for v1.1").

    join_style='mitre' keeps corners sharp (round would fillet them,
    which is wrong for a cut line meant to be sewn edge-to-edge).
    Buffer direction is outward for positive distance regardless of the
    input polygon's winding order, so this also fixes the old code's
    "assuming CCW? our points are mixed" uncertainty by construction.
    """
    if offset <= 0:
        return points[:]

    sign = 1 if is_outer else -1
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)

    buffered = poly.buffer(sign * offset, join_style="mitre", mitre_limit=5.0)

    if buffered.is_empty:
        return points[:]
    if buffered.geom_type == "MultiPolygon":
        # Shouldn't happen for a single dilated simple polygon, but if a
        # large negative offset ever splits the shape, keep the largest
        # piece rather than silently dropping the result.
        buffered = max(buffered.geoms, key=lambda g: g.area)

    buffered = orient(buffered, sign=1.0)  # keep consistent CCW winding
    coords = list(buffered.exterior.coords)
    if len(coords) > 1 and coords[0] == coords[-1]:
        coords = coords[:-1]  # drop shapely's closing duplicate point
    return coords


def add_seam_allowance(piece: PatternPiece, allowance: float) -> PatternPiece:
    """Returns a new piece with seam allowance added (outer offset).
    stitch_points is set to the ORIGINAL (pre-offset) outline - that's the
    sew line; the new, larger `points` is the cut line."""
    if allowance <= 0:
        return piece
    new_points = _offset_polygon(piece.points, allowance, is_outer=True)
    return PatternPiece(
        label=f"{piece.label} (SA {allowance}cm)",
        points=new_points,
        grain_angle=piece.grain_angle,
        stitch_points=piece.points,
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


def dress_front(m: PydanticMeasurements, flare: float = 0.0) -> PatternPiece:
    """
    One-piece dress front: the same bodice curve as bodice_front, continued
    straight down through the waist into a skirt instead of stopping at the
    waist seam. flare=0 gives a straight/sheath dress, flare>0 gives an
    a-line dress (aline_dress_front below just calls this with flare=6.0).
    """
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    quarter_bust = (m.bust_or_chest + m.ease) / 4
    quarter_waist = (m.waist + m.ease) / 4
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4
    nape_to_waist = m.back_length
    armhole_depth = m.bust_or_chest / 4 + 2.5
    dart_width = 2.5
    hip_depth = 20.0
    hem_y = -(nape_to_waist + m.skirt_length)

    points = [
        (0.0, 0.0),                                            # CF neck
        (shoulder / 2, -1.5),
        (quarter_bust * 0.7, -armhole_depth * 0.35),
        (quarter_bust, -armhole_depth * 0.55),
        (quarter_bust, -armhole_depth),
        (quarter_bust - dart_width / 2, -nape_to_waist + 2.5),
        (quarter_bust, -nape_to_waist),
        (quarter_waist, -nape_to_waist),
        (quarter_hip, -(nape_to_waist + hip_depth)),
        (quarter_hip + flare, hem_y),
        (0.0, hem_y),
    ]
    return PatternPiece(label="Dress Front", points=points, grain_angle=0.0)


def dress_back(m: PydanticMeasurements, flare: float = 0.0) -> PatternPiece:
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    quarter_bust = (m.bust_or_chest + m.ease) / 4
    quarter_waist = (m.waist + m.ease) / 4
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4
    nape_to_waist = m.back_length - 1.5
    armhole_depth = m.bust_or_chest / 4 + 2.0
    hip_depth = 20.0
    hem_y = -(nape_to_waist + m.skirt_length)

    points = [
        (0.0, 0.0),
        (shoulder / 2, -1.0),
        (quarter_bust * 0.7, -armhole_depth * 0.35),
        (quarter_bust, -armhole_depth * 0.55),
        (quarter_bust, -armhole_depth),
        (quarter_bust - 1.5, -nape_to_waist + 2.5),
        (quarter_bust, -nape_to_waist),
        (quarter_waist, -nape_to_waist),
        (quarter_hip, -(nape_to_waist + hip_depth)),
        (quarter_hip + flare, hem_y),
        (0.0, hem_y),
    ]
    return PatternPiece(label="Dress Back", points=points, grain_angle=0.0)


def tshirt_front(m: PydanticMeasurements) -> PatternPiece:
    """
    Boxier and looser than mens_shirt_front: t-shirts wear with more ease
    (+6cm on top of the user's own ease value) and no waist shaping, just a
    straight drop from armhole to hem.
    """
    quarter_chest = (m.bust_or_chest + m.ease + 6.0) / 4
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    armhole_depth = m.bust_or_chest / 4 + 2.0
    length = m.shirt_length

    points = [
        (0.0, 0.0),
        (shoulder / 2, -1.0),
        (quarter_chest * 0.85, -armhole_depth * 0.4),
        (quarter_chest, -armhole_depth),
        (quarter_chest, -length),
        (0.0, -length),
    ]
    return PatternPiece(label="T-Shirt Front", points=points, grain_angle=0.0)


def tshirt_back(m: PydanticMeasurements) -> PatternPiece:
    piece = tshirt_front(m)
    piece.label = "T-Shirt Back"
    return piece


def tshirt_sleeve(m: PydanticMeasurements) -> PatternPiece:
    # Short cap sleeve - a fixed fraction of sleeve_length, same approach
    # mens_shirt_sleeve_short takes for its short-sleeve variant.
    return set_in_sleeve(m, m.sleeve_length * 0.3, label="T-Shirt Sleeve")


def trouser_front(m: PydanticMeasurements, leg_length: float = None) -> PatternPiece:
    """
    Basic straight-leg trouser front. Front rise is shallower and the
    crotch curve narrower than the back piece, standard split between the
    two - the back needs the extra width for seat room, the front doesn't.
    leg_length overrides m.trouser_length - used by the knee-length
    breeches variant below, same fraction-of-measurement approach
    mens_shirt_sleeve_short takes for its short-sleeve variant.
    """
    quarter_waist = (m.waist + m.ease / 2) / 4
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4
    rise = m.rise
    length = m.trouser_length if leg_length is None else leg_length
    total_length = rise + length
    dart_width = 2.0
    crotch_x = quarter_hip * 0.22
    hem_out = quarter_hip * 0.75
    hem_in = quarter_hip * 0.30

    points = [
        (0.0, 0.0),                                     # CF waist
        (quarter_waist, 0.0),                            # side waist
        (quarter_waist - dart_width / 2, -rise * 0.35),  # waist dart notch
        (quarter_hip, -rise * 0.65),                     # side/hip point
        (hem_out, -total_length),                        # outseam hem
        (hem_in, -total_length),                         # inseam hem
        (crotch_x, -rise),                               # crotch point
    ]
    return PatternPiece(label="Trouser Front", points=points, grain_angle=0.0)


def trouser_back(m: PydanticMeasurements, leg_length: float = None) -> PatternPiece:
    quarter_waist = (m.waist + m.ease / 2) / 4
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4
    rise = m.rise * 1.08          # back rise sits deeper than front
    length = m.trouser_length if leg_length is None else leg_length
    total_length = rise + length
    dart_width = 3.0
    crotch_x = quarter_hip * 0.35  # wider back crotch curve for seat room
    hem_out = quarter_hip * 0.80
    hem_in = quarter_hip * 0.35

    points = [
        (0.0, 0.0),
        (quarter_waist + 1.0, 0.0),
        (quarter_waist + 1.0 - dart_width / 2, -rise * 0.35),
        (quarter_hip + 1.5, -rise * 0.65),
        (hem_out, -total_length),
        (hem_in, -total_length),
        (crotch_x, -rise),
    ]
    return PatternPiece(label="Trouser Back", points=points, grain_angle=0.0)


def breeches_front(m: PydanticMeasurements) -> PatternPiece:
    # Knee-length: a fixed fraction of trouser_length, same convention as
    # mens_shirt_sleeve_short - no separate field for it.
    piece = trouser_front(m, leg_length=m.trouser_length * 0.42)
    piece.label = "Breeches Front"
    return piece


def breeches_back(m: PydanticMeasurements) -> PatternPiece:
    piece = trouser_back(m, leg_length=m.trouser_length * 0.42)
    piece.label = "Breeches Back"
    return piece


def knickers_front(m: PydanticMeasurements) -> PatternPiece:
    """
    Basic brief-style front panel. Reuses m.rise (crotch depth) scaled way
    down - underwear only needs to cover the seat/crotch area, not the
    full trouser rise, so this takes a shallow fraction of the same
    measurement rather than adding a new field for it.
    """
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4
    depth = m.rise * 0.45
    side_width = quarter_hip * 0.55
    crotch_x = quarter_hip * 0.15
    leg_opening_x = quarter_hip * 0.38

    points = [
        (0.0, 0.0),                          # CF waistband point
        (side_width, -depth * 0.15),          # side, near top
        (side_width * 0.8, -depth * 0.65),    # leg opening curves out
        (leg_opening_x, -depth),              # leg opening low point
        (crotch_x, -depth * 0.8),             # crotch point, curves back in
    ]
    return PatternPiece(label="Knickers Front", points=points, grain_angle=0.0)


def knickers_back(m: PydanticMeasurements) -> PatternPiece:
    # Back panel covers more: deeper and wider than the front, same
    # front/back split logic the trouser pieces use for seat room.
    quarter_hip = ((m.hip or (m.waist + 20)) + m.ease) / 4
    depth = m.rise * 0.6
    side_width = quarter_hip * 0.6
    crotch_x = quarter_hip * 0.2
    leg_opening_x = quarter_hip * 0.42

    points = [
        (0.0, 0.0),
        (side_width, -depth * 0.15),
        (side_width * 0.8, -depth * 0.65),
        (leg_opening_x, -depth),
        (crotch_x, -depth * 0.8),
    ]
    return PatternPiece(label="Knickers Back", points=points, grain_angle=0.0)


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
           'mens_shirt_short_sleeve' | 'dress_straight' | 'dress_aline' |
           'tshirt' | 'mens_trousers' | 'mens_breeches' | 'knickers'
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
    elif style == "dress_straight":
        pieces = [dress_front(m, flare=0.0), dress_back(m, flare=0.0)]
    elif style == "dress_aline":
        pieces = [dress_front(m, flare=6.0), dress_back(m, flare=6.0)]
    elif style == "tshirt":
        pieces = [
            tshirt_front(m), tshirt_back(m),
            tshirt_sleeve(m), tshirt_sleeve(m),
        ]
    elif style == "mens_trousers":
        pieces = [trouser_front(m), trouser_back(m)]
    elif style == "mens_breeches":
        pieces = [breeches_front(m), breeches_back(m)]
    elif style == "knickers":
        pieces = [knickers_front(m), knickers_back(m)]
    else:
        raise ValueError(f"Unsupported style: {style}")

    if include_seam_allowance and seam_allowance_cm > 0:
        pieces = [add_seam_allowance(p, seam_allowance_cm) for p in pieces]

    return pieces
