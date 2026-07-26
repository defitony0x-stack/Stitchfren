import math
import sys
import os
import importlib.util

_geometry_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "nesting", "geometry.py",
)
_spec = importlib.util.spec_from_file_location("geometry", _geometry_path)
_geometry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_geometry)

polygon_area = _geometry.polygon_area
is_ccw = _geometry.is_ccw
ensure_ccw = _geometry.ensure_ccw
ear_clip_triangulate = _geometry.ear_clip_triangulate
minkowski_sum_convex = _geometry.minkowski_sum_convex
reflect_points = _geometry.reflect_points
signed_area = _geometry.signed_area

failures = []

def check(name, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}")
    if not cond:
        failures.append(name)

# --- polygon_area basic sanity ---
square = [(0,0),(10,0),(10,10),(0,10)]
check("square area = 100", abs(polygon_area(square) - 100) < 1e-6)

triangle = [(0,0),(4,0),(0,3)]
check("triangle area = 6", abs(polygon_area(triangle) - 6) < 1e-6)

# --- ear clipping: convex square -> 2 triangles, same total area ---
tris = ear_clip_triangulate(square)
check("square triangulates into 2 triangles", len(tris) == 2)
total = sum(polygon_area(t) for t in tris)
check("square triangle areas sum to 100", abs(total - 100) < 1e-6)

# --- ear clipping: concave dart shape ---
# Simulates a dart notch: a rectangle with a triangular notch cut into one edge
dart_shape = [(0,0), (10,0), (10,10), (6,10), (5,7), (4,10), (0,10)]
tris2 = ear_clip_triangulate(dart_shape)
expected_n_triangles = len(dart_shape) - 2  # simple polygon with n verts -> n-2 triangles
check(f"dart shape triangulates into {expected_n_triangles} triangles", len(tris2) == expected_n_triangles)
raw_area = polygon_area(dart_shape)
tri_area_sum = sum(polygon_area(t) for t in tris2)
check(f"dart shape triangle areas sum to polygon area ({raw_area:.4f})", abs(tri_area_sum - raw_area) < 1e-6)
# every triangle must have positive area (no degenerate slivers)
check("no degenerate (zero-area) triangles in dart shape", all(polygon_area(t) > 1e-6 for t in tris2))

# --- ear clipping: an actual bodice-like piece with a dart leg (more concave points) ---
# A simple (non-self-intersecting) polygon: a rectangle with a triangular dart
# notch poking up into the interior of the bottom edge (single boundary walk,
# no repeated non-adjacent vertices).
bodice_like = [
    (0, 0), (13, 0), (14, 3), (15, 0),  # dart notch
    (20, 0), (20, 25), (0, 25)
]
tris3 = ear_clip_triangulate(bodice_like)
raw_area3 = polygon_area(bodice_like)
tri_area_sum3 = sum(polygon_area(t) for t in tris3)
check("bodice-like concave shape area matches after triangulation",
      abs(tri_area_sum3 - raw_area3) < 1e-6)
check("bodice-like shape: all triangles non-degenerate", all(polygon_area(t) > 1e-9 for t in tris3))

# --- reflect_points ---
refl = reflect_points([(1,2),(3,4)], about=(0,0))
check("reflect about origin negates coords", refl == [(-1,-2),(-3,-4)])

# --- minkowski_sum_convex: two axis-aligned squares centered at origin ---
sq1 = [(-1,-1),(1,-1),(1,1),(-1,1)]  # 2x2 square centered at origin
sq2 = [(-2,-2),(2,-2),(2,2),(-2,2)]  # 4x4 square centered at origin
msum = minkowski_sum_convex(sq1, sq2)
msum_area = polygon_area(msum)
# Minkowski sum of two axis-aligned squares centered at origin with half-widths
# 1 and 2 is a square with half-width 3 -> 6x6 = area 36
check(f"square+square Minkowski sum area = 36 (got {msum_area})", abs(msum_area - 36) < 1e-6)
xs = [p[0] for p in msum]; ys = [p[1] for p in msum]
check("square+square Minkowski sum bounds are [-3,3]x[-3,3]",
      abs(min(xs)+3)<1e-6 and abs(max(xs)-3)<1e-6 and abs(min(ys)+3)<1e-6 and abs(max(ys)-3)<1e-6)

# --- minkowski_sum_convex: square + triangle (known via direct vertex enumeration) ---
sq = [(0,0),(2,0),(2,2),(0,2)]
tri = [(0,0),(1,0),(0,1)]
msum2 = minkowski_sum_convex(sq, tri)
# Minkowski sum of convex polygons P,Q always has area >= area(P)+area(Q) individually
# and its area should equal area(P) + area(Q) + mixed term. We validate via the
# well-known formula using the *mixed area* isn't trivial by hand, so instead
# validate by brute-force sampling: every p+q for p in P's vertices, q in Q's
# vertices must lie inside (or on boundary of) the computed Minkowski polygon.
def point_in_convex_polygon(pt, poly, eps=1e-6):
    poly = ensure_ccw(poly)
    n = len(poly)
    for i in range(n):
        a = poly[i]; b = poly[(i+1)%n]
        cross = (b[0]-a[0])*(pt[1]-a[1]) - (b[1]-a[1])*(pt[0]-a[0])
        if cross < -eps:
            return False
    return True

all_vertex_sums_covered = True
for p in sq:
    for q in tri:
        s = (p[0]+q[0], p[1]+q[1])
        if not point_in_convex_polygon(s, msum2):
            all_vertex_sums_covered = False
check("square+triangle Minkowski sum contains every vertex-pair sum p+q", all_vertex_sums_covered)
check("square+triangle Minkowski sum is convex (CCW turns consistent)",
      is_ccw(msum2) and all(
          ( (msum2[(i+1)%len(msum2)][0]-msum2[i][0])*(msum2[(i+2)%len(msum2)][1]-msum2[i][1])
          - (msum2[(i+1)%len(msum2)][1]-msum2[i][1])*(msum2[(i+2)%len(msum2)][0]-msum2[i][0]) ) >= -1e-6
          for i in range(len(msum2))
      ))

# --- minkowski_sum_convex: triangle + reflected triangle should be centrally symmetric hexagon ---
tri_a = [(0,0),(4,0),(0,3)]
tri_b_reflected = reflect_points(tri_a, about=(0,0))  # NFP-style: A ⊕ (-B)
nfp_self = minkowski_sum_convex(tri_a, tri_b_reflected)
# this polygon must contain the origin (since a - a = 0 for any vertex a shared placement),
# i.e. placing B at t=(0,0) exactly overlaps A (touching case at worst) -> origin must be
# inside or on the NFP boundary
check("triangle NFP-with-itself (A ⊕ -A) contains the origin (self-overlap position)",
      point_in_convex_polygon((0,0), nfp_self, eps=1e-6))

print()
if failures:
    print(f"{len(failures)} FAILURE(S):", failures)
else:
    print("ALL TESTS PASSED")
