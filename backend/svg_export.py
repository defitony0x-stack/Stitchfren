"""
Renders pattern pieces and nested layouts to SVG, for the frontend to
display directly (no client-side geometry logic needed, the backend
sends ready-to-render SVG).
"""

def _piece_path(points, offset_x=0, offset_y=0, scale=4, flip=False):
    """Convert a piece's point list into an SVG path 'd' string.
    scale: cm-to-px multiplier for display. flip: 180-degree rotation
    already applied by caller if needed, this just draws the given points."""
    pts = [(offset_x + x * scale, offset_y - y * scale) for x, y in points]
    d = f"M {pts[0][0]:.1f},{pts[0][1]:.1f} "
    for x, y in pts[1:]:
        d += f"L {x:.1f},{y:.1f} "
    d += "Z"
    return d


def render_pattern_pieces_svg(pieces, scale=4, gap=30):
    """Simple side-by-side preview of raw pattern pieces (not nested),
    used for the 'here's your pattern' view before cutting layout."""
    x_cursor = 20
    max_h = 0
    piece_svgs = []
    for p in pieces:
        xs = [pt[0] for pt in p.points]
        ys = [pt[1] for pt in p.points]
        w = (max(xs) - min(xs)) * scale
        h = (max(ys) - min(ys)) * scale
        max_h = max(max_h, h)

        d = _piece_path(p.points, offset_x=x_cursor - min(xs) * scale,
                         offset_y=20 + max(ys) * scale * -1 + h)
        piece_svgs.append(
            f'<path d="{d}" fill="#EDF2F4" stroke="#0B2545" stroke-width="1.5"/>'
            f'<text x="{x_cursor + w/2:.1f}" y="{h + 45:.1f}" '
            f'text-anchor="middle" font-family="IBM Plex Mono, monospace" '
            f'font-size="11" fill="#0B2545">{p.label}</text>'
            # grain arrow
            f'<line x1="{x_cursor + w/2:.1f}" y1="30" x2="{x_cursor + w/2:.1f}" y2="{h+10:.1f}" '
            f'stroke="#C9A24B" stroke-width="1.5" marker-end="url(#arrow)"/>'
        )
        x_cursor += w + gap

    total_w = x_cursor + 20
    total_h = max_h + 70

    return (
        f'<svg viewBox="0 0 {total_w:.0f} {total_h:.0f}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="4" refY="4" '
        f'orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#C9A24B"/></marker></defs>'
        f'<rect width="{total_w:.0f}" height="{total_h:.0f}" fill="none"/>'
        + "".join(piece_svgs) +
        '</svg>'
    )


def render_nested_layout_svg(piece_lookup, placements, fabric_width_cm,
                              fabric_length_cm, scale=4):
    """
    piece_lookup: dict label -> original points list
    placements: nesting.nest_pieces()['placements']
    Renders the actual cutting layout on a fabric-roll background grid.
    """
    from nesting import flip_points

    W = fabric_width_cm * scale
    H = fabric_length_cm * scale

    grid_lines = []
    step = 10 * scale  # 10cm grid
    x = 0
    while x <= W:
        grid_lines.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{H}" stroke="#274B75" stroke-width="0.5"/>')
        x += step
    y = 0
    while y <= H:
        grid_lines.append(f'<line x1="0" y1="{y}" x2="{W}" y2="{y}" stroke="#274B75" stroke-width="0.5"/>')
        y += step

    piece_svgs = []
    for pl in placements:
        base_points = piece_lookup[pl["label"]]
        pts = flip_points(base_points) if pl["rotated_180"] else base_points
        xs = [p[0] for p in pts]
        min_x, min_y = min(xs), min(p[1] for p in pts)

        ox = pl["x_offset_cm"] * scale
        oy_top = fabric_length_cm * scale - (pl["y_offset_cm"] * scale)

        d = _piece_path(pts, offset_x=ox - min_x * scale,
                         offset_y=oy_top + min_y * scale)
        piece_svgs.append(
            f'<path d="{d}" fill="#EDF2F4" fill-opacity="0.92" '
            f'stroke="#0B2545" stroke-width="1.2"/>'
        )
        cx = ox + (max(xs) - min_x) * scale / 2
        cy = oy_top - 6
        piece_svgs.append(
            f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" '
            f'font-family="IBM Plex Mono, monospace" font-size="10" '
            f'fill="#C9A24B">{pl["label"]}</text>'
        )

    return (
        f'<svg viewBox="0 0 {W:.0f} {H:.0f}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{W:.0f}" height="{H:.0f}" fill="#0B2545"/>'
        + "".join(grid_lines) + "".join(piece_svgs) +
        '</svg>'
    )
