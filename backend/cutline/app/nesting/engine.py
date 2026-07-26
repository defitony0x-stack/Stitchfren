"""
Stitchfren Nesting Engine v5 - True Minkowski-sum NFP.

Design (see PR notes / conversation for the full rationale):

  Layer 1 (app/nesting/geometry.py): pure-Python, no external deps.
    - ear-clip triangulation (handles concave dart notches)
    - Minkowski sum of two convex polygons (edge-angle merge)
  These are unit-tested in total isolation (test_geometry.py) against known
  areas/convexity/containment properties, with zero dependency on shapely.

  Layer 2 (this file): shapely, used ONLY for the well-trodden operations
  (polygon union/difference, point-in-polygon), never for the Minkowski
  sum math itself:
    - NFP(A, B) = union of minkowski_sum_convex(tri_a, tri_b) for every
      triangle pair, where B has been point-reflected about the origin
      first (A ⊕ (-B), the standard translational-NFP definition).
    - Free region for placing a piece = fabric strip minus the union of
      NFPs against every already-placed piece, for both the unrotated and
      180°-rotated orientation of the piece (grain-safe rotation only).
    - Deterministic placement = bottom-left-most vertex of that free
      region (the same rule production nesters like SVGnest/Deepnest use).
      No randomness anywhere in this file.

Coordinate contract (matches app/svg_export.py render_nested_layout_svg):
  x_offset_cm / y_offset_cm is the position of the placed piece's own
  bounding-box minimum corner within the fabric sheet, y measured from the
  bottom of the roll. This does NOT assume piece points are pre-normalized
  to (0,0) at their bbox corner (they aren't - see drafting/engine.py,
  where points run from y=0 down to y=-length).
"""

from __future__ import annotations

from typing import List, Dict, Any, Tuple, Optional

from shapely.geometry import Polygon, MultiPolygon, box, Point
from shapely.ops import unary_union

from app.models.schemas import NestingResult, NaiveResult, PiecePlacement
from .geometry import (
    ear_clip_triangulate,
    minkowski_sum_convex,
    reflect_points,
    polygon_area as _shoelace_area,
)

EPS = 1e-6
_INITIAL_Y_SEARCH_CM = 200.0
_Y_SEARCH_GROWTH = 1.6
_MAX_GROWTH_ATTEMPTS = 6


def flip_points(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    180-degree rotation of a raw point list about its own centroid.
    Public (the old private `_flip_polygon` only worked on Shapely objects)
    so svg_export can reconstruct the exact same flipped geometry that was
    used during nesting.
    """
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    return [(2 * cx - x, 2 * cy - y) for x, y in points]


def _bbox_min(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    return min(p[0] for p in points), min(p[1] for p in points)


def _bbox_max(points: List[Tuple[float, float]]) -> Tuple[float, float]:
    return max(p[0] for p in points), max(p[1] for p in points)


def _triangulate_cached(points, cache, key):
    if key not in cache:
        cache[key] = ear_clip_triangulate(points)
    return cache[key]


def _nfp_polygon(fixed_points, moving_points, tri_cache, fixed_key):
    """
    True Minkowski-sum NFP: NFP(fixed, moving) = fixed (+) (-moving)
    computed as the union of minkowski_sum_convex(tri_a, tri_b) over every
    pair of triangles from triangulate(fixed) x triangulate(reflect(moving)).
    Returns a shapely (Multi)Polygon, or None if degenerate.
    """
    ta = _triangulate_cached(fixed_points, tri_cache, fixed_key)
    reflected = reflect_points(moving_points, about=(0.0, 0.0))
    tb = ear_clip_triangulate(reflected)

    sums = []
    for a in ta:
        for b in tb:
            s = minkowski_sum_convex(a, b)
            if len(s) < 3:
                continue
            poly = Polygon(s)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if not poly.is_empty and poly.area > 1e-9:
                sums.append(poly)

    if not sums:
        return None
    return unary_union(sums)


def _candidate_points_from_region(valid_region):
    polys = list(valid_region.geoms) if hasattr(valid_region, "geoms") else [valid_region]
    pts = []
    for poly in polys:
        if poly.is_empty or not isinstance(poly, Polygon):
            continue
        rings = [poly.exterior] + list(poly.interiors)
        for ring in rings:
            for x, y in ring.coords:
                pts.append((x, y))
    return pts


def _best_placement_for_orientation(
    variant_points: List[Tuple[float, float]],
    placed_world_points: List[List[Tuple[float, float]]],
    tri_cache: Dict,
    fabric_width_cm: float,
    y_search_limit: float,
) -> Optional[Tuple[float, float]]:
    """
    Returns (tx, ty): the translation added directly to variant_points to
    get the final world-space polygon at the deterministic bottom-left-most
    valid position, or None if no valid placement exists within
    y_search_limit (caller grows the limit and retries).
    """
    min_x, min_y = _bbox_min(variant_points)
    max_x, _ = _bbox_max(variant_points)

    tx_lo = -min_x
    tx_hi = fabric_width_cm - max_x
    if tx_hi < tx_lo - EPS:
        return None  # piece is wider than the fabric roll, no orientation/limit helps

    ty_lo = -min_y

    valid_region = box(tx_lo, ty_lo, tx_hi, y_search_limit)

    excluded = []
    for i, wp in enumerate(placed_world_points):
        nfp = _nfp_polygon(wp, variant_points, tri_cache, ("placed", i))
        if nfp is not None and not nfp.is_empty:
            excluded.append(nfp)

    if excluded:
        excluded_union = unary_union(excluded)
        valid_region = valid_region.difference(excluded_union)

    if valid_region.is_empty:
        return None

    candidates = _candidate_points_from_region(valid_region)
    candidates.append((tx_lo, ty_lo))  # sheet's own bottom-left corner

    best = None
    for x, y in candidates:
        if x < tx_lo - EPS or x > tx_hi + EPS or y < ty_lo - EPS:
            continue
        if not valid_region.covers(Point(x, y)):
            continue
        key = (round(y, 4), round(x, 4))
        if best is None or key < best[0]:
            best = (key, (x, y))

    return best[1] if best else None


def nest_pieces(
    pieces: List[Dict[str, Any]],
    fabric_width_cm: float,
    margin_cm: float = 1.5,
) -> NestingResult:
    """
    Deterministic NFP-based nesting. Largest piece first (by exact polygon
    area), each placed at the bottom-left-most position of the true
    Minkowski-sum free region against every already-placed piece, trying
    both the unrotated and 180-degree-rotated (grain-safe) orientation.
    """
    pieces_sorted = sorted(pieces, key=lambda p: _shoelace_area(p["points"]), reverse=True)

    placed: List[Dict[str, Any]] = []
    placed_world_points: List[List[Tuple[float, float]]] = []
    tri_cache: Dict[Any, Any] = {}
    y_search_limit = _INITIAL_Y_SEARCH_CM

    for piece in pieces_sorted:
        raw_points = piece["points"]
        variants = [(raw_points, False), (flip_points(raw_points), True)]

        chosen = None  # (sort_key, tx, ty, variant_points, is_flipped)
        limit = y_search_limit
        attempts = 0
        while chosen is None and attempts <= _MAX_GROWTH_ATTEMPTS:
            for variant_points, is_flipped in variants:
                result = _best_placement_for_orientation(
                    variant_points, placed_world_points, tri_cache, fabric_width_cm, limit
                )
                if result is None:
                    continue
                tx, ty = result
                sort_key = (round(ty, 4), round(tx, 4))
                if chosen is None or sort_key < chosen[0]:
                    chosen = (sort_key, tx, ty, variant_points, is_flipped)
            if chosen is None:
                limit *= _Y_SEARCH_GROWTH
                attempts += 1

        if chosen is None:
            raise RuntimeError(
                f"Could not find a valid placement for piece '{piece['label']}' "
                f"on a {fabric_width_cm}cm-wide fabric roll (searched up to "
                f"{limit:.0f}cm of length). The piece is likely wider than "
                f"the fabric roll."
            )

        _, tx, ty, variant_points, is_flipped = chosen
        world_points = [(px + tx, py + ty) for px, py in variant_points]

        min_x, min_y = _bbox_min(variant_points)
        x_offset_cm = tx + min_x
        y_offset_cm = ty + min_y

        placed.append(
            {
                "label": piece["label"],
                "x_offset_cm": round(x_offset_cm, 2),
                "y_offset_cm": round(y_offset_cm, 2),
                "rotated_180": is_flipped,
                "world_points": world_points,
            }
        )
        placed_world_points.append(world_points)
        tri_cache[("placed", len(placed_world_points) - 1)] = ear_clip_triangulate(world_points)

        piece_max_y = max(p[1] for p in world_points)
        y_search_limit = max(y_search_limit, piece_max_y + 100.0)

    max_y = max(max(p[1] for p in pl["world_points"]) for pl in placed) + margin_cm
    total_area = sum(_shoelace_area(pl["world_points"]) for pl in placed)
    used_area = max_y * fabric_width_cm
    waste_pct = round(100 * (1 - total_area / used_area), 1) if used_area > 0 else 100.0

    placements = [
        PiecePlacement(
            label=p["label"],
            grid_row=0,
            grid_col=0,
            x_offset_cm=p["x_offset_cm"],
            y_offset_cm=p["y_offset_cm"],
            rotated_180=p["rotated_180"],
        )
        for p in placed
    ]

    return NestingResult(
        placements=placements,
        fabric_length_used_cm=round(max_y, 1),
        piece_area_cm2=round(total_area, 1),
        used_area_cm2=round(used_area, 1),
        waste_pct=waste_pct,
    )


def naive_layout_baseline(
    pieces: List[Dict], fabric_width_cm: float, gap_cm: float = 2.0
) -> NaiveResult:
    """Simple row-by-row bounding-box packing, used as the comparison
    baseline for fabric_saved_pct. Unchanged from the previous version -
    this function was already correct, it just wasn't being called
    (workers/tasks.py called nest_pieces() twice instead)."""
    x = y = row_h = max_y = 0.0
    total = 0.0

    for p in pieces:
        xs = [pt[0] for pt in p["points"]]
        ys = [pt[1] for pt in p["points"]]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        total += _shoelace_area(p["points"])

        if x + w > fabric_width_cm:
            x = 0
            y += row_h + gap_cm
            row_h = 0
        x += w + gap_cm
        row_h = max(row_h, h)
        max_y = max(max_y, y + h)

    fabric_len = max_y + gap_cm
    used = fabric_len * fabric_width_cm
    waste = round(100 * (1 - total / used), 1) if used > 0 else 100.0

    return NaiveResult(
        fabric_length_used_cm=round(fabric_len, 1),
        used_area_cm2=round(used, 1),
        waste_pct=waste,
    )
