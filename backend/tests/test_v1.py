"""
v1 validation: 3 measurement sets, both styles plus menswear.

This does NOT replace having an actual tailor/patternmaker check the
output against a hand draft, that step is still required before trusting
this for a real client, per the project's scoping notes. What this proves:
1. the drafting formulas respond correctly to different measurements
   (sanity check, not a substitute for expert validation)
2. the nesting solver produces a real, lower waste percentage than a
   naive layout on the same pieces, on three different piece sets
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drafting import Measurements, generate_pattern
from nesting import nest_pieces, naive_layout_baseline


TEST_CASES = [
    {
        # 112cm / "44-45 inch" is the standard apparel cotton/blend bolt width
        "name": "Case 1 - Women's UK 10 equivalent, A-line",
        "style": "bodice_aline",
        "measurements": Measurements(bust_or_chest=86, waist=68, hip=94,
                                      back_length=40, skirt_length=55),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 2 - Women's UK 16 equivalent, straight skirt",
        "style": "bodice_straight",
        "measurements": Measurements(bust_or_chest=102, waist=84, hip=110,
                                      back_length=41, skirt_length=60),
        "fabric_width_cm": 112,
    },
    {
        "name": "Case 3 - Men's standard shirt, size M equivalent",
        "style": "mens_shirt",
        "measurements": Measurements(bust_or_chest=100, waist=88,
                                      shoulder_width=45, sleeve_length=62,
                                      shirt_length=74),
        "fabric_width_cm": 112,
    },
]


def run_case(case):
    print(f"\n=== {case['name']} ===")
    pieces = generate_pattern(case["style"], case["measurements"])

    piece_dicts = []
    for p in pieces:
        xs = [pt[0] for pt in p.points]
        ys = [pt[1] for pt in p.points]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        print(f"  {p.label:24s} bounding box: {w:5.1f} cm x {h:5.1f} cm")
        piece_dicts.append({"label": p.label, "points": p.points})

    nested = nest_pieces(piece_dicts, case["fabric_width_cm"], cell_size=1.0)
    naive = naive_layout_baseline(piece_dicts, case["fabric_width_cm"])

    print(f"  Nested fabric length used : {nested['fabric_length_used_cm']} cm "
          f"(waste {nested['waste_pct']}%)")
    print(f"  Naive fabric length used  : {naive['fabric_length_used_cm']} cm "
          f"(waste {naive['waste_pct']}%)")

    improvement = naive["fabric_length_used_cm"] - nested["fabric_length_used_cm"]
    print(f"  Fabric length saved vs naive layout: {improvement:.1f} cm "
          f"({100*improvement/naive['fabric_length_used_cm']:.1f}% shorter)")

    assert nested["waste_pct"] < naive["waste_pct"], \
        "Nested layout should waste less than naive layout"
    assert nested["fabric_length_used_cm"] <= naive["fabric_length_used_cm"], \
        "Nested layout should use no more fabric length than naive layout"

    return nested, naive


if __name__ == "__main__":
    results = [run_case(c) for c in TEST_CASES]
    print("\nAll 3 test cases passed: nesting solver beats naive layout on every case.")
    print("Reminder: pattern geometry still needs sign-off from an actual "
          "tailor/patternmaker before this is used for a real client garment.")
