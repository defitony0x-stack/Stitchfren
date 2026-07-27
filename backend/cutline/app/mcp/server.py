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
  It only challenges an actual tools/call for draft_and_nest_pattern -
  initialize and tools/list stay free, because a buyer's agent (and OKX's
  own listing/evaluator) needs to discover the tool and its schema before
  it can ever construct a payment for it. Gating the whole /mcp path would
  make the server fail its own discovery handshake.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastmcp import FastMCP

from app.models.schemas import Measurements, PatternRequest, PatternStyle
from app.mcp.job import run_pattern_job, fallback_direction
from app.mcp.x402 import PaymentError, payment_required_body, verify_and_settle

mcp = FastMCP("Stitchfren")


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
) -> Dict[str, Any]:
    """
    Draft sloper pattern pieces from body measurements and nest them onto a
    fabric roll using true Minkowski-sum NFP placement. Returns a cut-ready
    DXF, pattern and layout SVGs, and a plain-language cutting sheet with
    verified fabric savings.

    style: one of bodice_aline, bodice_straight, bodice_aline_sleeved,
    bodice_top, skirt_straight, skirt_aline, mens_shirt,
    mens_shirt_short_sleeve. All measurements are in cm.
    """
    request = PatternRequest(
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
        ),
        fabric_width_cm=fabric_width_cm,
        include_seam_allowance=include_seam_allowance,
        seam_allowance_cm=seam_allowance_cm,
        allow_90_rotation=allow_90_rotation,
    )

    result = await run_pattern_job(request)
    message = (result.get("cutting_sheet") or {}).get("narrative") or fallback_direction(result)

    return {
        "ok": True,
        "message": message,
        "download_url": result.get("dxf_url"),
        "fabric_saved_cm": result.get("fabric_saved_cm"),
        "fabric_saved_pct": result.get("fabric_saved_pct"),
        "warnings": result.get("warnings", []),
        "cutting_sheet": result.get("cutting_sheet"),
        "result": result,
    }


# fastmcp's http_app() returns a Starlette ASGI app speaking the MCP
# Streamable HTTP transport at the given path. Mounted at "/mcp" in
# app/api/main.py, so the full route ends up being POST/GET /mcp/.
mcp_app = mcp.http_app(path="/")


def _paid_tool_call(body: bytes) -> bool:
    """
    True only if this POST body is a JSON-RPC tools/call for
    draft_and_nest_pattern specifically - initialize, tools/list, ping,
    etc. all pass through free. Deliberately conservative: anything that
    doesn't parse as the expected shape is treated as NOT a paid call and
    left to fastmcp/JSON-RPC to reject or handle on its own merits, so a
    parsing edge case here can only ever fail open into "let the MCP layer
    decide," never silently charge for something else.
    """
    try:
        payload = json.loads(body)
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("method") != "tools/call":
        return False
    params = payload.get("params") or {}
    return params.get("name") == "draft_and_nest_pattern"


class X402Gate:
    """
    Thin ASGI wrapper around mcp_app. Buffers the POST body (ASGI bodies can
    only be read once, so it has to be replayed to the wrapped app), checks
    whether this specific call is the priced tool, and if so runs the x402
    challenge/verify/settle flow from app/mcp/x402.py before letting the
    request through. Everything else (GET for the SSE stream, initialize,
    tools/list) passes straight through untouched.

    NOTE: only handles a single, non-batched JSON-RPC request per POST body
    - the common case for a tools/call. A batched-array request smuggling a
    tools/call alongside other methods would not be detected as paid here;
    fastmcp/JSON-RPC batching support should be checked before this gate is
    trusted with a real, non-trivial price.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        body_chunks = []
        more_body = True
        while more_body:
            message = await receive()
            body_chunks.append(message.get("body", b""))
            more_body = message.get("more_body", False)
        body = b"".join(body_chunks)

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        if _paid_tool_call(body):
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            x_payment = headers.get("x-payment")
            resource_url = f"{'https' if scope.get('scheme') != 'http' else 'http'}://{headers.get('host', '')}/mcp"

            try:
                if not x_payment:
                    raise PaymentError("X-PAYMENT header is required")
                await verify_and_settle(x_payment, resource_url)
            except PaymentError as exc:
                await _send_402(send, resource_url, str(exc))
                return

        await self.app(scope, replay_receive, send)


async def _send_402(send, resource_url: str, error: str) -> None:
    body_dict = payment_required_body(resource_url, error)
    body = json.dumps(body_dict).encode()
    await send({
        "type": "http.response.start",
        "status": 402,
        "headers": [
            (b"content-type", b"application/json"),
            (b"payment-required", json.dumps(body_dict).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


mcp_app_gated = X402Gate(mcp_app)
