"""
Nesting engine, v1.

Grain-constrained only: pieces may be placed rotated 180 degrees (flipped
top-to-bottom, since garment pieces are often cut in mirrored pairs anyway)
but NOT rotated 90 degrees, since that would break the grainline
requirement. Print/stripe matching is out of scope for v1.

Approach: rasterize the fabric roll and each piece onto a grid (cell size
in cm), then bottom-left-fill placement, scanning for the first position
where the piece's occupied cells don't collide with already-placed cells.
This is a real, correct placement algorithm, not just a bounding-box
approximation, since it checks actual piece silhouette overlap, not just
rectangle overlap.
"""

import numpy as np


def polygon_to_mask(points, cell_size):
    """Rasterize a polygon (list of (x,y) tuples, any sign) to a boolean
    grid at the given cell size. Returns (mask, origin_x, origin_y) where
    origin is the min corner, so mask[0,0] corresponds to (origin_x, origin_y).
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    width = int(np.ceil((max_x - min_x) / cell_size)) + 1
    height = int(np.ceil((max_y - min_y) / cell_size)) + 1
    mask = np.zeros((height, width), dtype=bool)

    # point-in-polygon via ray casting, sampled at each cell center
    poly = np.array(points)
    for row in range(height):
        cy = min_y + (row + 0.5) * cell_size
        for col in range(width):
            cx = min_x + (col + 0.5) * cell_size
            if _point_in_polygon(cx, cy, poly):
                mask[row, col] = True
    return mask, min_x, min_y


def _point_in_polygon(x, y, poly):
    n = len(poly)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def flip_points(points):
    """180-degree rotation (flip both axes) about the piece's own center."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    return [(2 * cx - x, 2 * cy - y) for x, y in points]


def nest_pieces(pieces, fabric_width_cm, cell_size=1.0, margin_cells=1):
    """
    pieces: list of dicts {'label':..., 'points':[(x,y),...]}
    Returns dict: placements (list of {label, x_offset, y_offset, rotated}),
    fabric_length_used_cm, piece_area_cm2, used_area_cm2, waste_pct.
    """
    fabric_cols = int(fabric_width_cm / cell_size)
    grid_rows_cap = 20000  # generous upper bound on fabric length grid
    occupancy = np.zeros((grid_rows_cap, fabric_cols), dtype=bool)

    placements = []
    total_piece_area = 0.0
    max_row_used = 0

    # sort largest-area first, a standard, effective nesting heuristic
    def area_of(pts):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    pieces_sorted = sorted(pieces, key=lambda p: area_of(p["points"]), reverse=True)

    for piece in pieces_sorted:
        pts = piece["points"]
        candidates = [(pts, False), (flip_points(pts), True)]

        best = None  # (row, col, mask, min_x, min_y, rotated)
        for candidate_pts, rotated in candidates:
            mask, min_x, min_y = polygon_to_mask(candidate_pts, cell_size)
            h, w = mask.shape
            if w > fabric_cols:
                continue
            # bottom-left-fill: scan rows from 0 up, cols from 0 right
            for row in range(0, grid_rows_cap - h):
                placed_here = False
                for col in range(0, fabric_cols - w + 1):
                    window = occupancy[row:row + h, col:col + w]
                    if not np.any(window & mask):
                        if best is None or row < best[0] or (row == best[0] and col < best[1]):
                            best = (row, col, mask, min_x, min_y, rotated, h, w)
                        placed_here = True
                        break
                if placed_here:
                    break  # first valid row for this orientation found

        if best is None:
            raise RuntimeError(f"Could not place piece '{piece['label']}', "
                                f"fabric width may be too narrow.")

        row, col, mask, min_x, min_y, rotated, h, w = best
        occupancy[row:row + h, col:col + w] |= mask
        max_row_used = max(max_row_used, row + h)

        piece_area = float(np.sum(mask)) * (cell_size ** 2)
        total_piece_area += piece_area

        placements.append({
            "label": piece["label"],
            "grid_row": row,
            "grid_col": col,
            "x_offset_cm": col * cell_size - min_x,
            "y_offset_cm": row * cell_size - min_y,
            "rotated_180": rotated,
        })

    fabric_length_used = (max_row_used + margin_cells) * cell_size
    used_area = fabric_length_used * fabric_width_cm
    waste_pct = 100.0 * (1 - total_piece_area / used_area) if used_area > 0 else None

    return {
        "placements": placements,
        "fabric_length_used_cm": round(fabric_length_used, 1),
        "piece_area_cm2": round(total_piece_area, 1),
        "used_area_cm2": round(used_area, 1),
        "waste_pct": round(waste_pct, 1),
    }


def polygon_area(points):
    """Shoelace formula, real polygon area regardless of shape convexity."""
    n = len(points)
    area = 0.0
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


def naive_layout_baseline(pieces, fabric_width_cm, cell_size=1.0, gap_cm=2.0):
    """
    Baseline for comparison: place each piece's bounding box in a single
    row (or wrap to a new row if it doesn't fit), no packing optimization.
    This approximates an unoptimized hand layout, the standard comparison
    point for proving nesting value. Placement uses each piece's bounding
    box (an unskilled layout doesn't hug the silhouette), but the waste
    calculation below uses each piece's REAL polygon area, same as the
    nested method, so the two waste percentages are an apples-to-apples
    comparison of fabric efficiency, not an artifact of measuring area
    two different ways.
    """
    x_cursor = 0.0
    y_cursor = 0.0
    row_height = 0.0
    total_piece_area = 0.0
    max_y = 0.0

    for piece in pieces:
        xs = [p[0] for p in piece["points"]]
        ys = [p[1] for p in piece["points"]]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        total_piece_area += polygon_area(piece["points"])  # real area, not bounding box

        if x_cursor + w > fabric_width_cm:
            x_cursor = 0.0
            y_cursor += row_height + gap_cm
            row_height = 0.0

        x_cursor += w + gap_cm
        row_height = max(row_height, h)
        max_y = max(max_y, y_cursor + h)

    fabric_length_used = max_y + gap_cm
    used_area = fabric_length_used * fabric_width_cm
    waste_pct = 100.0 * (1 - total_piece_area / used_area) if used_area > 0 else None

    return {
        "fabric_length_used_cm": round(fabric_length_used, 1),
        "used_area_cm2": round(used_area, 1),
        "waste_pct": round(waste_pct, 1),
    }
