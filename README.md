# Cut Line — Pattern Drafting & Nesting Agent (v1)

Turns client measurements into a real, sized pattern and a nested cutting
layout with a provable waste-percentage improvement over a naive layout.
Built as a candidate ASP (Agent Service Provider) for the OKX.AI Marketplace.

## What v1 actually does

- Drafts pattern pieces from standard body measurements, using published
  metric drafting formulas (the same family of formulas tools like
  Seamly2D encode), for three styles:
  - Women's bodice + A-line skirt
  - Women's bodice + straight skirt
  - Men's standard shirt
- Nests those pieces onto a fabric roll of a given width, grain-constrained
  (pieces may flip 180°, never rotate 90°, since that would break the
  grainline requirement)
- Reports fabric length used and waste percentage, validated against a
  naive (unoptimized) layout on the same pieces

## What v1 does NOT do (by design, see scoping notes)

- Draping-heavy styles, gathers, asymmetric cuts, complex necklines
- Fitted darts beyond a standard bust dart / waist darts / chest ease
- Sizes far outside standard adult proportions (extreme asymmetry, etc.)
- Print/stripe matching in nesting (planned after grain-only nesting is
  validated further)
- Streetwear/oversized blocks (a realistic v1.1 addition, see below)

**Before using this for a real client garment:** the generated pattern
pieces need sign-off from an actual tailor or patternmaker. The drafting
formulas here are a solid v1 approximation, not a replacement for expert
review, particularly for anything beyond the styles explicitly listed above.

## Project structure

```
dress-mvp/
├── backend/
│   ├── drafting.py       # pattern drafting formulas
│   ├── nesting.py        # grain-constrained nesting solver + naive baseline
│   ├── svg_export.py     # renders pieces/layouts to SVG for the frontend
│   ├── api.py            # Flask API tying it together
│   └── tests/
│       └── test_v1.py    # 3 validation cases, run before trusting output
├── frontend/
│   └── index.html        # landing page + live demo (calls the API)
├── requirements.txt
└── README.md
```

## Running it locally

```bash
cd dress-mvp
pip install -r requirements.txt
cd backend
python3 api.py          # starts the API on http://localhost:5000
```

Then open `frontend/index.html` directly in a browser (no build step,
it's a single static file). The demo form on the page calls the local API.

To run the validation tests:

```bash
cd backend
python3 tests/test_v1.py
```

## On the images provided as design reference

The stock photos supplied for visual direction (fabric fans, tailoring
close-ups) are copyrighted stock photography and are not reproduced here,
one had a visible Adobe Stock watermark. The hero background instead uses
an original CSS conic-gradient "fabric fan" motif in the same palette and
spirit, built from scratch rather than copied. If real photography is
wanted for the production listing, license it properly (Adobe Stock,
Unsplash for free-use images) or commission original photography.

## Next steps, roughly in order

1. **Get 2-3 real patternmaker sign-offs** on generated patterns against
   real client measurements, this is the actual credibility proof, more
   important than any additional feature.
2. **Tighten the nesting solver.** Current v1 beats a naive layout by
   ~50% consistently, but its absolute waste (16-31% in testing) is still
   above what a full No-Fit-Polygon algorithm (the approach used by
   SVGnest/Deepnest) would achieve. That's the highest-value technical
   upgrade before production use.
3. **LLM integration layer**, deliberately scoped narrow, to keep the
   real work in the solver rather than the language model:
   - *Input side:* parse a free-text request ("size 12 dress for a client,
     bust 90, waist 74...") into the structured measurement fields the
     API expects, instead of requiring a rigid form.
   - *Output side:* turn the raw nesting result into a plain-language
     cutting instruction sheet for whoever operates the cutter.
   - The LLM does not draft or nest anything itself, both of those stay
     in the deterministic solver code, per the "real work" architecture
     principle this whole project follows.
4. **Streetwear block** (tees, hoodies, joggers): boxier, higher-ease
   patterns, structurally closer to the men's shirt sloper already built
   than to the fitted bodice, a realistic v1.1 scope addition.
5. **OKX.AI Marketplace listing**: pay-per-call pricing per pattern
   generated, escrow release tied to patternmaker-verified accuracy once
   the reputation system is in place.
