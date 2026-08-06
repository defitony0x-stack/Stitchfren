"""
LLM layer for Stitchfren. Optional everywhere: every function degrades to a
rule-based fallback (or plain dict) when LLM_API_KEY isn't set, so the API
and worker both work with zero LLM configuration.

Talks to any OpenAI-compatible chat completions endpoint. .env.example ships
DeepSeek as the default (LLM_BASE_URL=https://api.deepseek.com/v1), but any
provider using the same request/response shape works by changing the three
LLM_* env vars.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

import httpx

from ..models.schemas import PatternStyle

LLM_API_KEY = os.getenv("LLM_API_KEY")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

_TIMEOUT_SECONDS = 20.0

# --- Rule-based fallback: matches things like "bust 92", "waist: 74cm",
# "shoulder width 40 cm", AND more conversational phrasing like "waist sits
# around 74" or "hip is roughly 98" - the \D window has to be wide enough
# to span filler words ("sits around", "is roughly") between the keyword
# and the number, without needing an LLM call at all. ---
_MEASUREMENT_PATTERNS = {
    # bust_or_chest tries two directions: "bust 92" / "chest is 92" (keyword
    # before number), and "92 up top" (number before keyword) - "up top"
    # never comes before the number the way bust/chest/waist/hip do.
    "bust_or_chest": [
        r"(?:bust|chest)\D{0,25}(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\D{0,10}up top",
    ],
    "waist": [r"waist\D{0,25}(\d+(?:\.\d+)?)"],
    "hip": [r"hip\D{0,25}(\d+(?:\.\d+)?)"],
    "back_length": [r"back[\s_-]?length\D{0,25}(\d+(?:\.\d+)?)"],
    "skirt_length": [r"skirt[\s_-]?length\D{0,25}(\d+(?:\.\d+)?)"],
    "shoulder_width": [r"shoulder(?:[\s_-]?width)?\D{0,25}(\d+(?:\.\d+)?)"],
    "sleeve_length": [r"sleeve[\s_-]?length\D{0,25}(\d+(?:\.\d+)?)"],
    "shirt_length": [r"shirt[\s_-]?length\D{0,25}(\d+(?:\.\d+)?)"],
    "ease": [r"\bease\D{0,25}(\d+(?:\.\d+)?)"],
}

# --- Job parameters: not body measurements, but still worth pulling out of
# the same free text so "on 60cm fabric, make 8" fills the Fabric width /
# Quantity fields alongside the measurements, instead of only measurements
# being covered. Kept as a separate dict from _MEASUREMENT_PATTERNS (which
# maps onto the Measurements pydantic model 1:1) since these two map onto
# PatternRequest's own fields instead - the parser doesn't care about that
# distinction, but it matters to anyone reading this file later. ---
_JOB_PARAM_PATTERNS = {
    "fabric_width_cm": [
        r"fabric[\s_-]?width\D{0,25}(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*cm[\s-]?(?:wide|width)\b",
        r"on\s+(\d+(?:\.\d+)?)\s*cm\s+fabric",
    ],
    "quantity": [
        r"(?:quantity|qty)\D{0,10}(\d+)",
        r"(?:make|need|cut|produce)\D{0,10}(\d+)",
        r"run of\D{0,5}(\d+)",
        r"batch of\D{0,5}(\d+)",
        r"(\d+)\s*(?:pieces|units|copies|garments)\b",
    ],
}

_ALL_TEXT_PATTERNS = {**_MEASUREMENT_PATTERNS, **_JOB_PARAM_PATTERNS}


def parse_measurements_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Regex fallback used when the LLM is unconfigured or fails. Returns
    numeric measurement/job-parameter fields plus an optional "style"
    string key (see guess_style_from_text) - the str/float mix is
    intentional, not a bug.

    "quantity" defaults to 1 whenever anything else in the text parses
    successfully, even if the text never mentions a quantity - a single
    garment is a safer default than leaving the field unfilled (and
    ambiguous) after an otherwise-successful parse.
    """
    lowered = text.lower()
    found: Dict[str, Any] = {}
    for field, patterns in _ALL_TEXT_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                try:
                    found[field] = float(match.group(1))
                    break
                except ValueError:
                    continue
    style = guess_style_from_text(text)
    if style:
        found["style"] = style
    if found and "quantity" not in found:
        found["quantity"] = 1.0
    return found or None


# --- Style guess: ordered (style, [patterns]) pairs, checked top to bottom,
# first match wins. Order matters - more specific phrasing has to be checked
# before the generic garment word it contains (e.g. "short sleeve shirt"
# before bare "shirt", "a-line skirt" before bare "skirt") or the specific
# case never gets a chance to match. Used both as the fallback when the LLM
# is unconfigured/fails, and to validate whatever style the LLM guessed. ---
_STYLE_PATTERNS: list[tuple[PatternStyle, list[str]]] = [
    # t-shirt has to be checked before the generic \bshirt\b pattern below,
    # since "t-shirt" contains a word-boundary-delimited "shirt" and would
    # otherwise match mens_shirt first.
    (PatternStyle.tshirt, [r"t[\s-]?shirt"]),
    (PatternStyle.mens_shirt_short_sleeve, [r"short[\s-]?sleeve"]),
    (PatternStyle.mens_shirt, [r"men'?s?\s+shirt", r"\bshirt\b"]),
    (PatternStyle.bodice_aline_sleeved, [r"bodice.{0,20}sleev", r"sleev.{0,20}bodice"]),
    (PatternStyle.bodice_aline, [r"a[\s-]?line.{0,20}bodice", r"bodice.{0,20}a[\s-]?line"]),
    # Bare "top" is deliberately not matched here - it collides with the
    # "92 up top" bust/chest phrasing in _MEASUREMENT_PATTERNS, so a plain
    # "top" mention only counts when it's clearly attached to "bodice".
    (PatternStyle.bodice_top, [r"bodice.{0,10}top"]),
    (PatternStyle.bodice_straight, [r"\bbodice\b"]),
    (PatternStyle.dress_aline, [r"a[\s-]?line.{0,20}dress", r"dress.{0,20}a[\s-]?line"]),
    (PatternStyle.dress_straight, [r"\bdress\b"]),
    (PatternStyle.skirt_aline, [r"a[\s-]?line.{0,20}skirt", r"skirt.{0,20}a[\s-]?line"]),
    (PatternStyle.skirt_straight, [r"\bskirt\b"]),
    (PatternStyle.mens_breeches, [r"\bbreeches\b"]),
    (PatternStyle.knickers, [r"\bknickers\b"]),
    (PatternStyle.mens_trousers, [r"\btrousers?\b", r"\bpants\b"]),
]


def guess_style_from_text(text: str) -> Optional[str]:
    """
    Best-effort style guess from free text, used as: (a) the fallback when
    the LLM is unconfigured or its own style guess doesn't validate, and
    (b) the value returned to the frontend when only the rule-based
    measurement parser ran. Returns a valid PatternStyle string or None -
    never guessing is safer than guessing wrong, so this only returns a
    value when a pattern actually matches.
    """
    lowered = text.lower()
    for style, patterns in _STYLE_PATTERNS:
        for pattern in patterns:
            if re.search(pattern, lowered):
                return style.value
    return None


def _strip_code_fence(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
    return content.strip()


async def _chat_completion(prompt: str, temperature: float = 0.0) -> Optional[str]:
    if not LLM_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": temperature,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError):
        return None


async def enhance_with_llm(text: str) -> Optional[Dict[str, Any]]:
    """
    Free-text -> structured measurements via LLM. Returns None (not an
    exception) on any failure so callers fall through to the rule-based
    parser in parse_measurements_from_text - see app/api/main.py parse_text().
    """
    valid_styles = ", ".join(s.value for s in PatternStyle)
    prompt = (
        "Extract sewing job details in centimeters from the text below: "
        "body measurements, the fabric width being cut on, and how many "
        "garments to make. Respond with ONLY a raw JSON object (no markdown "
        "fences, no prose) using any of these keys you can confidently "
        "find: bust_or_chest, waist, hip, back_length, skirt_length, "
        "shoulder_width, sleeve_length, shirt_length, ease, "
        "fabric_width_cm, quantity. Omit keys you can't find. "
        "Also include a \"style\" key with your best guess of the garment "
        f"being described, using exactly one of these values: {valid_styles}. "
        "Omit \"style\" entirely if you're not reasonably confident.\n\n"
        f"Text: {text}"
    )
    content = await _chat_completion(prompt)
    if not content:
        return None

    try:
        parsed = json.loads(_strip_code_fence(content))
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict) or not parsed:
        return None

    # Only keep numeric values for known fields - never trust the LLM to
    # only emit the schema we asked for.
    cleaned = {}
    for key in _ALL_TEXT_PATTERNS:
        if key in parsed:
            try:
                cleaned[key] = float(parsed[key])
            except (TypeError, ValueError):
                continue

    # Style is a free-text field from the LLM's point of view, so validate
    # it against the real enum before trusting it - an invalid/hallucinated
    # value falls back to the same keyword guesser used when there's no LLM
    # at all, rather than being dropped silently.
    llm_style = parsed.get("style")
    valid_values = {s.value for s in PatternStyle}
    if isinstance(llm_style, str) and llm_style in valid_values:
        cleaned["style"] = llm_style
    else:
        guessed = guess_style_from_text(text)
        if guessed:
            cleaned["style"] = guessed

    # Same "quantity defaults to 1 if the text didn't say" rule as the
    # rule-based path - see parse_measurements_from_text.
    if cleaned and "quantity" not in cleaned:
        cleaned["quantity"] = 1.0

    return cleaned or None


async def generate_cutting_sheet(
    request,
    nested,
    naive,
    fabric_saved_cm: float,
    fabric_saved_pct: float,
    skip_llm: bool = False,
) -> Dict[str, Any]:
    """
    Builds the cutting sheet returned alongside a pattern job's result.
    Always returns the rule-based sheet; adds an LLM-written plain-language
    "narrative" field on top of it only if LLM_API_KEY is configured, the
    call succeeds, AND skip_llm is False. Never raises - a flaky LLM call
    should never fail an otherwise-successful nesting job.

    skip_llm=True is used by the free draft_and_nest_pattern_preview MCP
    tool (app/mcp/server.py) - it's the one real per-call dollar cost in the
    pipeline beyond hosting, so the free tier skips it outright rather than
    calling it and discarding the result.
    """
    sheet: Dict[str, Any] = {
        "style": request.style.value,
        "quantity": getattr(request, "quantity", 1),
        "fabric_width_cm": request.fabric_width_cm,
        "fabric_length_needed_cm": nested.fabric_length_used_cm,
        "naive_fabric_length_cm": naive.fabric_length_used_cm,
        "estimated_savings_cm": fabric_saved_cm,
        "estimated_savings_pct": fabric_saved_pct,
        "pieces": [p.label for p in nested.placements],
        "seam_allowance_cm": request.seam_allowance_cm if request.include_seam_allowance else 0,
        "notes": [
            "Cut all pieces with fabric right sides together unless noted.",
            "Transfer notches and grainlines before unpinning each piece.",
        ],
    }

    if skip_llm:
        return sheet

    prompt = (
        "Write a short plain-language cutting instruction sheet (under 150 "
        "words, no markdown, no headers) for a home sewer, based on this "
        f"JSON summary of their pattern job:\n\n{json.dumps(sheet)}"
    )
    narrative = await _chat_completion(prompt, temperature=0.4)
    if narrative:
        sheet["narrative"] = narrative.strip()

    return sheet
