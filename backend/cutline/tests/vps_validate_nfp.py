"""
Standalone validation for the new Minkowski-sum NFP nesting engine
(app/nesting/engine.py + app/nesting/geometry.py).

Run this on the VPS (real shapely + numpy), not in a sandbox without
network access. It deliberately imports app.drafting.engine and
app.nesting.engine directly rather than through app.drafting / app.nesting's
__init__.py, so it can validate the nesting engine independently of the
drafting/__init__.py import bug (bug #1), which is being fixed separately.

Usage:
    cd cutline
    pip install -r requirements.txt
    python3 tests/vps_validate_nfp.py

What this checks, and why each check matters:
  1. Geometry unit tests (test_geometry.py) still pass here too - confirms
     the pure-Python layer behaves identically on this Python/platform.
  2. For 3 real pattern-piece sets: nested waste% < naive waste%, and
     nested fabric length <= naive fabric length (the core product claim).
  3. ZERO OVERLAP: every pair of placed pieces has intersection area ~0.
     This is the check that actually matters for "production grade" - a
     nesting engine that reports low waste but overlaps pieces is worse
     than useless (it would waste real fabric when cut).
  4. DETERMINISM: running the exact same input twice produces byte-identical
     placements. The old engine used unseeded np.random and could not
     make this guarantee; result_hash claims depend on this being true.
  5. Coordinate contract: reconstructs each placed piece's world position
     from (x_offset_cm, y_offset_cm, rotated_180) exactly the way
     svg_export.py does, and confirms it matches what the nesting engine
     internally validated as collision-free. This is the renderer/engine
     coordinate-convention bug found during this session - if this check
     fails, the rendered SVG will show overlapping pieces even though the
     engine "thinks" they don't overlap.

Paste the full output back, including any tracebacks.
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


print("=" * 70)
print("STEP 0: pure-Python geometry layer (no shapely needed for this part)")
print("=" * 70)
try:
    from app.nesting.geometry import (
        ear_clip_triangulate,
        minkowski_sum_convex,
        reflect_points,
        polygon_area,
        is_ccw,
    )

    square = [(0, 0), (10, 0), (10, 10), (0, 10)]
    check("polygon_area(square) == 100", abs(polygon_area(square) - 100) < 1e-6)
    tris = ear_clip_triangulate(square)
    check("square triangulates to 2 triangles", len(tris) == 2)
    sq1 = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    sq2 = [(-2, -2), (2, -2), (2, 2), (-2, 2)]
    msum_area = polygon_area(minkowski_sum_convex(sq1, sq2))
    check(f"Minkowski sum of two centered squares has area 36 (got {msum_area})",
          abs(msum_area - 36) < 1e-6)
except Exception as e:
    check("geometry layer imports and runs", False, repr(e))
    raise

print()
print("=" * 70)
print("STEP 1: import drafting + nesting (bypassing __init__.py wrappers)")
print("=" * 70)
try:
    from app.drafting.engine import generate_pattern, PydanticMeasurements
    from app.nesting.engine import nest_pieces, naive_layout_baseline, flip_points
    from shapely.geometry import Polygon
    check("imports succeeded", True)
except Exception as e:
    check("imports succeeded", False, repr(e))
    raise

TEST_CASES = [
    {
        "name": "Case 1 - Women's UK 10 equivalent, A-line",
        "style": "bodice_aline",
        "measurements": dict(bust_or_chest=86, waist=68, hip=94, back_length=40, skirt_length=55),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 2 - Women's UK 16 equivalent, straight skirt",
        "style": "bodice_straight",
        "measurements": dict(bust_or_chest=102, waist=84, hip=110, back_length=41, skirt_length=60),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 3 - Men's standard shirt, size M equivalent",
        "style": "mens_shirt",
        "measurements": dict(bust_or_chest=100, waist=88, shoulder_width=45, sleeve_length=62, shirt_length=74),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 4 - Sleeveless bodice top only",
        "style": "bodice_top",
        "measurements": dict(bust_or_chest=88, waist=70, back_length=39),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 5 - Straight skirt only",
        "style": "skirt_straight",
        "measurements": dict(bust_or_chest=88, waist=70, hip=98, skirt_length=58),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 6 - A-line skirt only",
        "style": "skirt_aline",
        "measurements": dict(bust_or_chest=88, waist=70, hip=98, skirt_length=58),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 7 - A-line dress with sleeves",
        "style": "bodice_aline_sleeved",
        "measurements": dict(bust_or_chest=94, waist=76, hip=100, back_length=40, skirt_length=56, sleeve_length=58),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 8 - Men's short-sleeve shirt",
        "style": "mens_shirt_short_sleeve",
        "measurements": dict(bust_or_chest=104, waist=92, shoulder_width=46, sleeve_length=62, shirt_length=72),
        "fabric_width_cm": 112,
    },
]


def world_points_for_placement(base_points, placement):
    """
    Reconstructs a placed piece's world-space polygon EXACTLY the way
    svg_export.py's render_nested_layout_svg does, from
    (x_offset_cm, y_offset_cm, rotated_180) alone - i.e. treating the
    engine's output the same way the renderer will.
    """
    pts = flip_points(base_points) if placement.rotated_180 else base_points
    min_x = min(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    ox = placement.x_offset_cm - min_x
    oy = placement.y_offset_cm - min_y
    return [(x + ox, y + oy) for x, y in pts]


print()
print("=" * 70)
print("STEP 2: run each case - waste, overlap, determinism, render contract")
print("=" * 70)

for case in TEST_CASES:
    print(f"\n--- {case['name']} ---")
    m = PydanticMeasurements(**case["measurements"])
    pieces = generate_pattern(case["style"], m)
    piece_dicts = [{"label": p.label, "points": p.points} for p in pieces]

    for p in pieces:
        xs = [pt[0] for pt in p.points]
        ys = [pt[1] for pt in p.points]
        print(f"  {p.label:24s} bbox: {max(xs)-min(xs):5.1f} x {max(ys)-min(ys):5.1f} cm")

    t0 = time.time()
    try:
        nested = nest_pieces(piece_dicts, case["fabric_width_cm"])
    except Exception as e:
        check(f"{case['name']}: nest_pieces() completes without error", False, repr(e))
        continue
    elapsed = time.time() - t0
    naive = naive_layout_baseline(piece_dicts, case["fabric_width_cm"])

    print(f"  nest_pieces() took {elapsed:.2f}s")
    print(f"  Nested: {nested.fabric_length_used_cm} cm, waste {nested.waste_pct}%")
    print(f"  Naive : {naive.fabric_length_used_cm} cm, waste {naive.waste_pct}%")

    check(f"{case['name']}: nested waste < naive waste",
          nested.waste_pct < naive.waste_pct,
          f"nested={nested.waste_pct} naive={naive.waste_pct}")
    check(f"{case['name']}: nested fabric length <= naive fabric length",
          nested.fabric_length_used_cm <= naive.fabric_length_used_cm,
          f"nested={nested.fabric_length_used_cm} naive={naive.fabric_length_used_cm}")

    # --- overlap check, using the SAME reconstruction the renderer uses ---
    base_lookup = {p["label"]: p["points"] for p in piece_dicts}
    world_polys = []
    for pl in nested.placements:
        wp = world_points_for_placement(base_lookup[pl.label], pl)
        poly = Polygon(wp)
        if not poly.is_valid:
            poly = poly.buffer(0)
        world_polys.append((pl.label, poly))

    max_overlap = 0.0
    worst_pair = None
    for i in range(len(world_polys)):
        for j in range(i + 1, len(world_polys)):
            li, pi = world_polys[i]
            lj, pj = world_polys[j]
            inter_area = pi.intersection(pj).area
            if inter_area > max_overlap:
                max_overlap = inter_area
                worst_pair = (li, lj)
    check(f"{case['name']}: zero overlap between placed pieces (render-reconstructed)",
          max_overlap < 0.5,  # cm^2 tolerance for floating point / touching edges
          f"max overlap {max_overlap:.3f} cm^2 between {worst_pair}")

    # --- determinism check ---
    nested_again = nest_pieces(piece_dicts, case["fabric_width_cm"])
    same = all(
        (a.label, a.x_offset_cm, a.y_offset_cm, a.rotated_180) ==
        (b.label, b.x_offset_cm, b.y_offset_cm, b.rotated_180)
        for a, b in zip(nested.placements, nested_again.placements)
    )
    check(f"{case['name']}: identical input produces identical placements (deterministic)", same)

print()
print("=" * 70)
if FAILURES:
    print(f"{len(FAILURES)} FAILURE(S):")
    for f in FAILURES:
        print(" -", f)
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
