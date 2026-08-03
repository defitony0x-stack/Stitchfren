"""
Real MCP protocol server for Stitchfren, mounted at /mcp on the same FastAPI
app as the existing REST API (app/api/main.py). This replaces "a REST route
named /mcp/draft-and-nest" (backend/mcp-gateway's approach) with an actual
MCP server that answers initialize / tools/list / tools/call, which is what
an MCP-speaking client - including whatever probed
POST /mcp/initialize, /sse, /.well-known/mcp on the plain cutline deploy and
got 404s for all of them - actually expects at a stable /mcp path.

Requires the `fastmcp` package (added to requirements.txt).

Design decisions, and why:

- One tool: draft_and_nest_pattern. Matches the README's "one real job this
  backend does" framing exactly - no separate draft-only or nest-only tools.
- The tool body calls app.mcp.job.run_pattern_job() directly and awaits it,
  so a buyer's single tools/call gets the finished DXF/SVG/cutting-sheet
  result inline - no polling, per the A2MCP requirement that the endpoint be
  self-contained. The old Celery/poll path in app/workers/tasks.py and
  app/api/main.py's /api/pattern + /api/status are untouched, for the
  existing frontend demo and any other direct REST caller.
- Payment gating happens in X402Gate below, NOT inside the tool function.
  initialize/notifications/initialized/ping always stay free (session
  bootstrap), and tools/list is free once a session exists (i.e. after a
  free initialize) - so a real client can complete the handshake and
  discover both tools, including the free preview tool, without paying.
  A bare/sessionless tools/list POST (an automated x402 prober, not a real
  MCP client) is still priced - see X402Gate/_is_free for the exact rule
  and why.
- X402Gate no longer calls the OKX facilitator itself. A "paid" request now
  gets handed to a real x402.http.middleware.fastapi.PaymentMiddlewareASGI
  instance (built in app/mcp/x402.py's build_paid_app()) instead of this
  class calling facilitator.verify()/.settle() directly - see x402.py's
  module docstring for why that changed. What X402Gate still does, because
  no SDK has an equivalent for it: inspect the JSON-RPC method in the body
  to decide which of the two apps (free passthrough vs. middleware-wrapped)
  a given POST /mcp call should go to.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from app.models.schemas import Measurements, PatternRequest, PatternStyle
from app.mcp.job import run_pattern_job, fallback_direction, watermark_svg
from app.mcp.x402 import build_paid_app

mcp = FastMCP("Stitchfren")


def _build_request(
    style: str,
    bust_or_chest: float,
    waist: float,
    fabric_width_cm: float,
    hip: Optional[float],
    ease: float,
    back_length: float,
    skirt_length: float,
    shoulder_width: Optional[float],
    sleeve_length: float,
    shirt_length: float,
    include_seam_allowance: bool,
    seam_allowance_cm: float,
    allow_90_rotation: bool,
    quantity: int = 1,
    rise: float = 26.0,
    trouser_length: float = 75.0,
) -> PatternRequest:
    """Shared by both tools below so the paid and free-preview inputs can
    never quietly drift apart into two different request shapes."""
    return PatternRequest(
        style=PatternStyle(style),
        measurements=Measurements(
            bust_or_chest=bust_or_chest,
            waist=waist,
            hip=hip,
            ease=ease,
            back_length=back_length,
            skirt_length=skirt_length,
            shoulder_width=shoulder_width,
            sleeve_length=sleeve_length,
            shirt_length=shirt_length,
            rise=rise,
            trouser_length=trouser_length,
        ),
        fabric_width_cm=fabric_width_cm,
        include_seam_allowance=include_seam_allowance,
        seam_allowance_cm=seam_allowance_cm,
        allow_90_rotation=allow_90_rotation,
        quantity=quantity,
    )


@mcp.tool
async def draft_and_nest_pattern(
    style: str,
    bust_or_chest: float,
    waist: float,
    fabric_width_cm: float,
    hip: Optional[float] = None,
    ease: float = 2.0,
    back_length: float = 40.0,
    skirt_length: float = 55.0,
    shoulder_width: Optional[float] = None,
    sleeve_length: float = 60.0,
    shirt_length: float = 70.0,
    include_seam_allowance: bool = True,
    seam_allowance_cm: float = 1.0,
    allow_90_rotation: bool = False,
    quantity: int = 1,
    rise: float = 26.0,
    trouser_length: float = 75.0,
) -> Dict[str, Any]:
    """
    Draft sloper pattern pieces from body measurements and nest them onto a
    fabric roll using true Minkowski-sum NFP placement. Returns a cut-ready
    DXF, pattern and layout SVGs, and a plain-language cutting sheet with
    verified fabric savings. Price: 0.5 USDT per call.

    style: one of bodice_aline, bodice_straight, bodice_aline_sleeved,
    bodice_top, skirt_straight, skirt_aline, mens_shirt,
    mens_shirt_short_sleeve, dress_straight, dress_aline, tshirt,
    mens_trousers, mens_breeches, knickers. All measurements are in cm.

    quantity: how many copies of this garment to nest together in one
    layout, e.g. 50 for a small production run (default 1, max 50 - this
    nester does true NFP placement, not a coarse bulk packer).

    rise, trouser_length: mens_trousers and mens_breeches only. rise is
    waist-to-crotch depth, trouser_length is crotch-to-hem (inseam) for
    mens_trousers, or knee-length (a fixed fraction of trouser_length) for
    mens_breeches. knickers uses rise too, scaled down, but ignores
    trouser_length. All three params are ignored by every other style.

    Not sure it's worth it yet? Call draft_and_nest_pattern_preview first -
    same inputs, free, everything except the DXF.
    """
    request = _build_request(
        style, bust_or_chest, waist, fabric_width_cm, hip, ease, back_length,
        skirt_length, shoulder_width, sleeve_length, shirt_length,
        include_seam_allowance, seam_allowance_cm, allow_90_rotation, quantity,
        rise, trouser_length,
    )

    result = await run_pattern_job(request)
    message = (result.get("cutting_sheet") or {}).get("narrative") or fallback_direction(result)

    return {
        "ok": True,
        "message": message,
        "download_url": result.get("dxf_url"),
        "pattern_svg_url": result.get("pattern_svg_url"),
        "layout_svg_url": result.get("layout_svg_url"),
        "fabric_saved_cm": result.get("fabric_saved_cm"),
        "fabric_saved_pct": result.get("fabric_saved_pct"),
        "warnings": result.get("warnings", []),
        "cutting_sheet": result.get("cutting_sheet"),
        "result": result,
    }


@mcp.tool
async def draft_and_nest_pattern_preview(
    style: str,
    bust_or_chest: float,
    waist: float,
    fabric_width_cm: float,
    hip: Optional[float] = None,
    ease: float = 2.0,
    back_length: float = 40.0,
    skirt_length: float = 55.0,
    shoulder_width: Optional[float] = None,
    sleeve_length: float = 60.0,
    shirt_length: float = 70.0,
    include_seam_allowance: bool = True,
    seam_allowance_cm: float = 1.0,
    allow_90_rotation: bool = False,
    quantity: int = 1,
    rise: float = 26.0,
    trouser_length: float = 75.0,
) -> Dict[str, Any]:
    """
    FREE preview of draft_and_nest_pattern - same inputs, same drafting and
    nesting math, same verified fabric-savings numbers. Two things are
    withheld: no DXF (download_url is always null), and the returned SVGs
    carry a "PREVIEW - NOT TO SCALE" watermark. No LLM-written narrative
    either, to keep this tool free to run. Call draft_and_nest_pattern
    (0.5 USDT) for the real, unwatermarked, cut-ready output.
    """
    request = _build_request(
        style, bust_or_chest, waist, fabric_width_cm, hip, ease, back_length,
        skirt_length, shoulder_width, sleeve_length, shirt_length,
        include_seam_allowance, seam_allowance_cm, allow_90_rotation, quantity,
        rise, trouser_length,
    )

    result = await run_pattern_job(request, skip_llm=True)

    return {
        "ok": True,
        "message": (
            "This is a preview - drafting and nesting numbers are real, but "
            "the DXF is withheld and the SVGs are watermarked. Call "
            "draft_and_nest_pattern (0.5 USDT) for the cut-ready DXF."
        ),
        "download_url": None,
        "pattern_svg": watermark_svg(result.get("pattern_svg", "")),
        "layout_svg": watermark_svg(result.get("layout_svg", "")),
        "fabric_saved_cm": result.get("fabric_saved_cm"),
        "fabric_saved_pct": result.get("fabric_saved_pct"),
        "warnings": result.get("warnings", []),
        "cutting_sheet": result.get("cutting_sheet"),
    }


# fastmcp's http_app() returns a Starlette ASGI app speaking the MCP
# Streamable HTTP transport at the given path. Mounted at "/mcp" in
# app/api/main.py, so the full route ends up being POST/GET /mcp/.
mcp_app = mcp.http_app(path="/")


# Methods that must stay reachable with NO payment, because they're what a
# client needs just to bootstrap/maintain an MCP session - not because
# they're "the free part of the product." tools/list is deliberately NOT in
# this set (see _is_free's docstring).
FREE_METHODS = {"initialize", "notifications/initialized", "ping"}

# Tool names callable with no payment even though their method (tools/call)
# is otherwise priced. Just the preview tool for now.
FREE_TOOL_NAMES = {"draft_and_nest_pattern_preview"}


def _is_free(body: bytes, headers: Dict[str, str]) -> bool:
    """
    Default-DENY: only explicit session-bootstrap plumbing (FREE_METHODS),
    an in-session tools/list, or a call to a tool we've deliberately made
    free (FREE_TOOL_NAMES) counts as free. Everything else, including a
    bare/sessionless tools/list and non-JSON-RPC probe bodies, is "not
    free" and gets routed to the PaymentMiddlewareASGI-wrapped app instead
    of fastmcp directly. OKX's own x402-check prober doesn't do a full MCP
    handshake before probing for pricing; it expects ANY unauthenticated
    hit on a route=x402 resource to come back as a 402 with accepts[], not
    a protocol-level 400. That's the literal ask in the "absence of x402
    challenge" report: GET, a generic POST body, and a bare tools/list POST
    should all get 402, not 400.

    One real tradeoff this creates: a bare, sessionless tools/list can't
    discover the tool schemas (including that the free preview tool
    exists) without paying. The in-session check below mitigates this for
    real MCP clients specifically - tools/list is free once a session
    exists (i.e. after a free initialize), while a stateless probe
    tools/list the way OKX's x402-check tool sends one is still priced.
    """
    try:
        payload = json.loads(body)
    except Exception:
        return False  # unparseable / non-JSON-RPC probe body -> priced path
    if not isinstance(payload, dict):
        return False

    method = payload.get("method")
    if method in FREE_METHODS:
        return True
    if method == "tools/list" and "mcp-session-id" in headers:
        return True
    if method == "tools/call":
        params = payload.get("params") or {}
        if params.get("name") in FREE_TOOL_NAMES:
            return True
    return False


class AcceptFixer:
    """fastmcp's Streamable HTTP transport answers HTTP 406 ("Client must
    accept both application/json and text/event-stream") to any request whose
    Accept header lacks `text/event-stream`. Some clients and automated
    checkers (including OKX's x402 endpoint validator) don't send that
    header, so they get a 406 and the endpoint reads as "invalid" even though
    the MCP server is healthy and a real paid call works fine.

    This thin ASGI wrapper rewrites the Accept header to include
    `text/event-stream` before fastmcp sees the request, so those probes get
    a genuine MCP response (200) instead of a 406. A real buyer client already
    sends the header, so its flow is unchanged. The X402Gate still runs first
    and returns the 402 challenge on an unpaid priced call, so payment gating
    is unaffected.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            new_headers = []
            had_accept = False
            for k, v in scope.get("headers", []):
                if k.lower() == b"accept":
                    had_accept = True
                    # fastmcp's Accept check requires BOTH `application/json`
                    # and `text/event-stream` to be present (it uses
                    # startswith() on each, so a bare `*/*` is not enough).
                    # Normalize to exactly those two so any client or
                    # automated checker (incl. OKX's x402 endpoint validator,
                    # which sends no SSE Accept header) gets a real MCP
                    # response instead of HTTP 406 "Not Acceptable".
                    new_headers.append((k, b"application/json, text/event-stream"))
                else:
                    new_headers.append((k, v))
            if not had_accept:
                new_headers.append((b"accept", b"application/json, text/event-stream"))
            scope = dict(scope)
            scope["headers"] = new_headers
        await self.app(scope, receive, send)


class X402Gate:
    """
    Thin ASGI dispatcher in front of two inner apps: `free_app` (fastmcp,
    Accept-header-fixed, no payment gating) and `paid_app` (the same thing
    wrapped in a real PaymentMiddlewareASGI - see x402.py's build_paid_app).
    Buffers the POST body (ASGI bodies can only be read once) so it can
    inspect the JSON-RPC method before deciding which app to forward to,
    then replays the body to whichever app it picks, unchanged.

    This class no longer talks to the OKX facilitator itself, and no longer
    builds the 402 body by hand for POST requests - paid_app's
    PaymentMiddlewareASGI does both of those now. What's left here is only
    the JSON-RPC method classification, which has no SDK equivalent because
    the SDK's routing model is one price per "METHOD /path" and this
    service multiplexes several JSON-RPC methods over one POST /mcp path.

    NOTE: only handles a single, non-batched JSON-RPC request per POST body
    - the common case for a tools/call. A batched-array request smuggling a
    tools/call alongside other methods would not be detected as paid here;
    fastmcp/JSON-RPC batching support should be checked before this gate is
    trusted with a real, non-trivial price.
    """

    def __init__(self, free_app, paid_app):
        self.free_app = free_app
        self.paid_app = paid_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.free_app(scope, receive, send)
            return

        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

        if scope["method"] == "GET":
            # A GET carrying mcp-session-id is the SSE stream for a session
            # that was already bootstrapped via a (free) initialize POST -
            # let it through untouched. A bare GET with no session-id is
            # exactly the kind of stateless probe OKX's x402-check tool
            # sends; route it to paid_app so PaymentMiddlewareASGI answers
            # with its own 402 challenge, instead of this class building
            # one by hand.
            if "mcp-session-id" not in headers:
                await self.paid_app(scope, receive, send)
            else:
                await self.free_app(scope, receive, send)
            return

        if scope["method"] != "POST":
            await self.free_app(scope, receive, send)
            return

        body_chunks = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)

        # Some automated checkers (incl. OKX's x402 endpoint validator) POST
        # an `initialize` with empty/missing `params`, which fastmcp rejects
        # with JSON-RPC -32602 ("Invalid request parameters") and surfaces to
        # the checker as HTTP 400. Default the params for a bare initialize so
        # the handshake completes and the endpoint reads as valid. A real MCP
        # client sends full params and is unaffected.
        try:
            parsed = json.loads(body)
            if (
                isinstance(parsed, dict)
                and parsed.get("method") == "initialize"
                and not parsed.get("params")
            ):
                parsed["params"] = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "okx-check", "version": "1.0"},
                }
                body = json.dumps(parsed).encode()
        except Exception:
            pass

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            # Body's already fully drained above, so anything the app asks
            # for after this is it watching for a REAL disconnect - forward
            # to the original receive instead of manufacturing one. Hard-
            # coding {"type": "http.disconnect"} here previously made
            # fastmcp's Streamable HTTP transport think the client had gone
            # away mid-response, so it accepted the initialize/tools
            # handshake (headers + session id already sent) and then
            # aborted before writing any body - the "200 with an empty
            # body" failure an earlier probe found.
            return await receive()

        target = self.paid_app if not _is_free(body, headers) else self.free_app
        await target(scope, replay_receive, send)


# AcceptFixer runs first on both paths so fastmcp never 406s on headers.
# free_app skips payment gating entirely; paid_app is the same fastmcp app
# wrapped in a real PaymentMiddlewareASGI, built in x402.py, which is what
# OKX's listing check looks for. X402Gate decides which of the two a given
# request goes to (see its docstring for why that decision can't move into
# the SDK).
_free_app = AcceptFixer(mcp_app)
_paid_app = build_paid_app(
    AcceptFixer(mcp_app),
    resource_description=(
        "Draft sloper pattern pieces from body measurements, nest them "
        "onto a fabric roll (true NFP placement), and return SVGs, a "
        "cut-ready DXF, and a cutting sheet with verified fabric savings."
    ),
)
mcp_app_gated = X402Gate(_free_app, _paid_app)
