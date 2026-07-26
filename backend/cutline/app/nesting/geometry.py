"""
Pure-Python geometric primitives for NFP nesting. No external dependencies
(not even numpy) so these can be unit-tested in total isolation from shapely.

Contains:
- polygon_area (shoelace)
- signed_area / is_ccw / ensure_ccw
- ear_clip_triangulate: triangulates a simple polygon (convex or concave),
  required because pattern pieces have concave dart notches.
- minkowski_sum_convex: Minkowski sum of two CONVEX polygons via the
  standard "merge edge vectors by angle" O(n+m) algorithm.
"""

import math

EPS = 1e-9


def signed_area(points):
    n = len(points)
    a = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return a / 2.0


def polygon_area(points):
    return abs(signed_area(points))


def is_ccw(points):
    return signed_area(points) > 0


def ensure_ccw(points):
    return points[:] if is_ccw(points) else list(reversed(points))


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _point_in_triangle(p, a, b, c):
    """True if p is strictly inside triangle abc (CCW)."""
    d1 = _cross(a, b, p)
    d2 = _cross(b, c, p)
    d3 = _cross(c, a, p)
    has_neg = (d1 < -EPS) or (d2 < -EPS) or (d3 < -EPS)
    has_pos = (d1 > EPS) or (d2 > EPS) or (d3 > EPS)
    return not (has_neg and has_pos)


def ear_clip_triangulate(points):
    """
    Ear-clipping triangulation for a simple polygon (may be concave, e.g.
    the dart-notch pieces). Returns a list of triangles, each a list of
    3 (x,y) tuples. Works for any simple (non-self-intersecting) polygon.
    """
    poly = ensure_ccw(list(points))
    # de-duplicate consecutive identical points
    cleaned = []
    for p in poly:
        if not cleaned or (abs(p[0] - cleaned[-1][0]) > EPS or abs(p[1] - cleaned[-1][1]) > EPS):
            cleaned.append(p)
    if len(cleaned) > 1 and abs(cleaned[0][0] - cleaned[-1][0]) < EPS and abs(cleaned[0][1] - cleaned[-1][1]) < EPS:
        cleaned.pop()
    poly = cleaned

    if len(poly) < 3:
        return []
    if len(poly) == 3:
        return [poly]

    indices = list(range(len(poly)))
    triangles = []
    guard = 0
    max_guard = len(poly) * len(poly) + 10

    while len(indices) > 3 and guard < max_guard:
        guard += 1
        ear_found = False
        n = len(indices)
        for k in range(n):
            i_prev = indices[(k - 1) % n]
            i_curr = indices[k]
            i_next = indices[(k + 1) % n]
            a, b, c = poly[i_prev], poly[i_curr], poly[i_next]

            # must be convex vertex (CCW => positive cross for a valid ear tip)
            if _cross(a, b, c) <= EPS:
                continue

            # no other polygon vertex may lie inside this candidate ear
            has_point_inside = False
            for idx in indices:
                if idx in (i_prev, i_curr, i_next):
                    continue
                if _point_in_triangle(poly[idx], a, b, c):
                    has_point_inside = True
                    break

            if not has_point_inside:
                triangles.append([a, b, c])
                indices.pop(k)
                ear_found = True
                break

        if not ear_found:
            # numerical fallback: clip the sharpest remaining convex vertex
            # (keeps the algorithm total instead of raising on edge cases)
            best_k, best_cross = None, -1
            n = len(indices)
            for k in range(n):
                i_prev = indices[(k - 1) % n]
                i_curr = indices[k]
                i_next = indices[(k + 1) % n]
                a, b, c = poly[i_prev], poly[i_curr], poly[i_next]
                cr = _cross(a, b, c)
                if cr > best_cross:
                    best_cross = cr
                    best_k = k
            if best_k is None:
                break
            i_prev = indices[(best_k - 1) % len(indices)]
            i_curr = indices[best_k]
            i_next = indices[(best_k + 1) % len(indices)]
            triangles.append([poly[i_prev], poly[i_curr], poly[i_next]])
            indices.pop(best_k)

    if len(indices) == 3:
        triangles.append([poly[indices[0]], poly[indices[1]], poly[indices[2]]])

    return triangles


def minkowski_sum_convex(P, Q):
    """
    Minkowski sum of two CONVEX polygons P and Q (list of (x,y), CCW).
    Standard O(n+m) algorithm: merge edge vectors of both polygons sorted
    by angle, starting from the bottom-most (then left-most) vertex of
    each, accumulating the summed boundary.
    """
    P = ensure_ccw(list(P))
    Q = ensure_ccw(list(Q))

    def start_index(poly):
        # bottom-most, then left-most point
        return min(range(len(poly)), key=lambda i: (poly[i][1], poly[i][0]))

    pi = start_index(P)
    qi = start_index(Q)
    P = P[pi:] + P[:pi]
    Q = Q[qi:] + Q[:qi]

    def edges(poly):
        n = len(poly)
        return [(poly[(i + 1) % n][0] - poly[i][0], poly[(i + 1) % n][1] - poly[i][1]) for i in range(n)]

    Pe = edges(P)
    Qe = edges(Q)

    def angle(v):
        a = math.atan2(v[1], v[0])
        return a if a >= -EPS else a + 2 * math.pi

    i = j = 0
    result = [(P[0][0] + Q[0][0], P[0][1] + Q[0][1])]
    while i < len(Pe) or j < len(Qe):
        if i >= len(Pe):
            v = Qe[j]; j += 1
        elif j >= len(Qe):
            v = Pe[i]; i += 1
        else:
            ap, aq = angle(Pe[i]), angle(Qe[j])
            if ap < aq - EPS:
                v = Pe[i]; i += 1
            elif aq < ap - EPS:
                v = Qe[j]; j += 1
            else:
                # collinear edges: merge them into one step
                v = (Pe[i][0] + Qe[j][0], Pe[i][1] + Qe[j][1])
                i += 1; j += 1
        last = result[-1]
        result.append((last[0] + v[0], last[1] + v[1]))

    result.pop()  # last point should coincide with the first (closed loop)
    return result


def reflect_points(points, about=(0.0, 0.0)):
    ax, ay = about
    return [(2 * ax - x, 2 * ay - y) for x, y in points]
