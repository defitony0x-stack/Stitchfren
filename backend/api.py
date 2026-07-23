"""
Flask API for the pattern drafting + nesting engine.
Run with: python3 api.py  (serves on http://localhost:5000)
"""

from flask import Flask, request, jsonify

from drafting import Measurements, generate_pattern
from nesting import nest_pieces, naive_layout_baseline
from svg_export import render_pattern_pieces_svg, render_nested_layout_svg

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/pattern", methods=["POST", "OPTIONS"])
def api_pattern():
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(force=True)

    try:
        m = Measurements(
            bust_or_chest=float(data["bust_or_chest"]),
            waist=float(data["waist"]),
            hip=float(data["hip"]) if data.get("hip") else None,
            back_length=float(data.get("back_length", 40.0)),
            shoulder_width=float(data["shoulder_width"]) if data.get("shoulder_width") else None,
            sleeve_length=float(data.get("sleeve_length", 58.0)),
            skirt_length=float(data.get("skirt_length", 55.0)),
            shirt_length=float(data.get("shirt_length", 70.0)),
        )
        style = data["style"]
        fabric_width = float(data.get("fabric_width_cm", 112))

        pieces = generate_pattern(style, m)
        piece_dicts = [{"label": p.label, "points": p.points} for p in pieces]
        piece_lookup = {p.label: p.points for p in pieces}

        nested = nest_pieces(piece_dicts, fabric_width, cell_size=1.0)
        naive = naive_layout_baseline(piece_dicts, fabric_width)

        pattern_svg = render_pattern_pieces_svg(pieces)
        layout_svg = render_nested_layout_svg(
            piece_lookup, nested["placements"], fabric_width,
            nested["fabric_length_used_cm"]
        )

        return jsonify({
            "ok": True,
            "pattern_svg": pattern_svg,
            "layout_svg": layout_svg,
            "nested": nested,
            "naive": naive,
            "fabric_saved_cm": round(naive["fabric_length_used_cm"] - nested["fabric_length_used_cm"], 1),
            "fabric_saved_pct": round(100 * (naive["fabric_length_used_cm"] - nested["fabric_length_used_cm"])
                                       / naive["fabric_length_used_cm"], 1),
        })
    except KeyError as e:
        return jsonify({"ok": False, "error": f"Missing required field: {e}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
