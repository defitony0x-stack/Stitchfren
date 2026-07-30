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


def parse_measurements_from_text(text: str) -> Optional[Dict[str, float]]:
    """Regex fallback used when the LLM is unconfigured or fails."""
    lowered = text.lower()
    found: Dict[str, float] = {}
    for field, patterns in _MEASUREMENT_PATTERNS.items():
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                try:
                    found[field] = float(match.group(1))
                    break
                except ValueError:
                    continue
    return found or None


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
    prompt = (
        "Extract sewing body measurements in centimeters from the text below. "
        "Respond with ONLY a raw JSON object (no markdown fences, no prose) "
        "using any of these keys you can confidently find: bust_or_chest, "
        "waist, hip, back_length, skirt_length, shoulder_width, "
        "sleeve_length, shirt_length, ease. Omit keys you can't find.\n\n"
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
    for key in _MEASUREMENT_PATTERNS:
        if key in parsed:
            try:
                cleaned[key] = float(parsed[key])
            except (TypeError, ValueError):
                continue
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
