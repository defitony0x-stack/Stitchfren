"""
Pattern drafting engine.

Implements standard block/sloper drafting formulas from published metric
patternmaking methods (the same family of formulas tools like Seamly2D
encode). These are simplified approximations suitable for a v1, standard
adult proportions only. Anything outside standard size ranges, or any
draping/asymmetric style, is explicitly out of scope, see project notes.

All measurements in centimeters. All outputs are pattern pieces: a dict
with 'label', 'points' (ordered list of (x, y) tuples tracing the piece
outline, x=horizontal, y=vertical), and 'grain_angle' (0 = vertical grain,
the standard case for these pieces).
"""

from dataclasses import dataclass, field


@dataclass
class Measurements:
    bust_or_chest: float
    waist: float
    hip: float = None
    back_length: float = 40.0      # nape to waist
    shoulder_width: float = None    # full shoulder, seam to seam
    sleeve_length: float = 58.0
    skirt_length: float = 55.0
    shirt_length: float = 70.0
    height: float = 165.0
    ease: float = 6.0               # wearing ease, cm added to body measurement


@dataclass
class PatternPiece:
    label: str
    points: list          # list of (x, y) tuples, closed polygon
    grain_angle: float = 0.0   # 0 = vertical (standard)
    seam_pairs: list = field(default_factory=list)  # for future print matching


def _default_shoulder(bust_or_chest):
    # standard proportion approximation: shoulder ~ 1/5 of bust/chest + constant
    return bust_or_chest / 5 + 4.5


def bodice_front(m: Measurements) -> PatternPiece:
    """Standard fitted bodice front block with a single bust dart."""
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    quarter_bust = (m.bust_or_chest + m.ease) / 4
    quarter_waist = (m.waist + m.ease) / 4
    nape_to_waist = m.back_length
    armhole_depth = m.bust_or_chest / 4 + 2.5  # scye depth approximation
    dart_width = 2.5  # standard bust dart intake

    # Origin at center front neck point, y increases downward for drafting
    # convention, we flip to a standard y-up coordinate system for output.
    points = [
        (0, 0),                                        # CF neck
        (shoulder / 2, -1.5),                           # shoulder point
        (quarter_bust, -armhole_depth * 0.4),           # underarm curve start
        (quarter_bust, -armhole_depth),                 # underarm point
        (quarter_bust - dart_width / 2, -nape_to_waist + 3),  # dart leg
        (quarter_bust, -nape_to_waist),                  # side waist
        (quarter_waist, -nape_to_waist),                 # CF waist (with dart take-in)
        (0, -nape_to_waist),                             # CF waist point
    ]
    return PatternPiece(label="Bodice Front", points=points, grain_angle=0.0)


def bodice_back(m: Measurements) -> PatternPiece:
    """Standard fitted bodice back block, smaller dart than front, no bust dart."""
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    quarter_bust = (m.bust_or_chest + m.ease) / 4
    quarter_waist = (m.waist + m.ease) / 4
    nape_to_waist = m.back_length - 1.5  # back is slightly shorter than front
    armhole_depth = m.bust_or_chest / 4 + 2.0

    points = [
        (0, 0),
        (shoulder / 2, -1.0),
        (quarter_bust, -armhole_depth * 0.4),
        (quarter_bust, -armhole_depth),
        (quarter_bust - 1.5, -nape_to_waist + 3),
        (quarter_bust, -nape_to_waist),
        (quarter_waist, -nape_to_waist),
        (0, -nape_to_waist),
    ]
    return PatternPiece(label="Bodice Back", points=points, grain_angle=0.0)


def straight_skirt_front_or_back(m: Measurements, is_front: bool) -> PatternPiece:
    """One panel (front or back) of a basic straight skirt block."""
    quarter_waist = (m.waist + m.ease / 2) / 4
    quarter_hip = (m.hip + m.ease) / 4
    length = m.skirt_length
    dart_width = 2.0 if is_front else 2.5
    hip_depth = 20.0  # standard waist-to-hip drop

    points = [
        (0, 0),
        (quarter_waist, 0),
        (quarter_waist - dart_width / 2, -hip_depth * 0.4),
        (quarter_hip, -hip_depth),
        (quarter_hip, -length),
        (0, -length),
    ]
    label = "Skirt Front (Straight)" if is_front else "Skirt Back (Straight)"
    return PatternPiece(label=label, points=points, grain_angle=0.0)


def aline_skirt_front_or_back(m: Measurements, is_front: bool, flare: float = 6.0) -> PatternPiece:
    """
    A-line variant: same waist/hip fit as the straight skirt, hem widened
    by `flare` cm per panel, dart intake reduced since some fullness is
    released into the flare instead of a dart.
    """
    base = straight_skirt_front_or_back(m, is_front)
    pts = base.points
    # widen the hem points (last two before closing) by `flare`
    hem_hip_x, hem_hip_y = pts[3]
    hem_x, hem_y = pts[4]
    new_points = pts[:3] + [
        (hem_hip_x, hem_hip_y),
        (hem_x + flare, hem_y),
        (pts[5][0], pts[5][1]),
    ]
    label = "Skirt Front (A-Line)" if is_front else "Skirt Back (A-Line)"
    return PatternPiece(label=label, points=new_points, grain_angle=0.0)


def mens_shirt_front(m: Measurements) -> PatternPiece:
    quarter_chest = (m.bust_or_chest + m.ease) / 4
    shoulder = m.shoulder_width or _default_shoulder(m.bust_or_chest)
    armhole_depth = m.bust_or_chest / 4 + 3.0
    length = m.shirt_length

    points = [
        (0, 0),
        (shoulder / 2, -1.0),
        (quarter_chest, -armhole_depth * 0.4),
        (quarter_chest, -armhole_depth),
        (quarter_chest, -length),
        (0, -length),
    ]
    return PatternPiece(label="Shirt Front", points=points, grain_angle=0.0)


def mens_shirt_back(m: Measurements) -> PatternPiece:
    piece = mens_shirt_front(m)
    piece.label = "Shirt Back"
    return piece


def mens_shirt_sleeve(m: Measurements) -> PatternPiece:
    sleeve_len = m.sleeve_length
    cap_height = m.bust_or_chest / 10 + 4
    width = m.bust_or_chest / 4 + 6

    points = [
        (0, 0),
        (width / 2, cap_height),
        (width, 0),
        (width * 0.8, -sleeve_len),
        (width * 0.2, -sleeve_len),
    ]
    return PatternPiece(label="Sleeve", points=points, grain_angle=0.0)


def generate_pattern(style: str, m: Measurements) -> list:
    """
    style: 'bodice_aline' | 'bodice_straight' | 'mens_shirt'
    Returns list of PatternPiece.
    """
    if style == "bodice_straight":
        return [
            bodice_front(m), bodice_back(m),
            straight_skirt_front_or_back(m, True),
            straight_skirt_front_or_back(m, False),
        ]
    elif style == "bodice_aline":
        return [
            bodice_front(m), bodice_back(m),
            aline_skirt_front_or_back(m, True),
            aline_skirt_front_or_back(m, False),
        ]
    elif style == "mens_shirt":
        return [
            mens_shirt_front(m), mens_shirt_back(m), mens_shirt_sleeve(m), mens_shirt_sleeve(m),
        ]
    else:
        raise ValueError(f"Unsupported style for v1: {style}")
