"""
x402 payment gate for Stitchfren's MCP tool.

Same OKX facilitator this project's Node gateway (backend/mcp-gateway) talks
to, ported to Python so the paid tool call and the pattern-drafting code it
protects can live in one process/one deploy instead of two.

The shapes below are verified against OKX's own live Onchain OS docs for
this exact facilitator (web3.okx.com/onchainos/dev-docs/payments/
api-http-onetime, "exact" scheme section), not just the generic x402 spec -
OKX's facilitator speaks x402 protocol v2, which differs from v1 in several
field names. Audited 2026-07-27; re-check against OKX's docs if this stops
working, in case their API has moved since.

STILL UNVERIFIED - flagged rather than guessed at:
  - PRICE_ATOMIC assumes USD\u20ae0 uses 6 decimals (matching USDC/USDT
    convention - block explorer data is consistent with this but it hasn't
    been confirmed via a direct decimals() contract call).
  - extra.version="1" is USD\u20ae0's own EIP-712 domain version, a
    token-contract detail, not the protocol version - unconfirmed against
    the token contract itself. Wrong here breaks client-side signing
    (invalid_signature on verify), not this server's own requests.
Do one real paid call in staging and watch for invalid_signature /
param_mismatch / requirements_mismatch errorReason values before trusting
either assumption at volume.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

import httpx

NETWORK = "eip155:196"  # X Layer, same chain the Node gateway settles on
FACILITATOR_BASE = "https://web3.okx.com"
FACILITATOR_PATH_PREFIX = "/api/v6/pay/x402"
FACILITATOR_URL = FACILITATOR_BASE + FACILITATOR_PATH_PREFIX
ASSET_NAME = "USD\u20ae0"

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS", "0xac27574ce229cbe4f6a48d7195566dd32f3a1fbb")
USDT0_ASSET_ADDRESS = os.getenv("USDT0_ASSET_ADDRESS", "0x779ded0c9e1022225f8e0630b35a9b54be713736")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")

# $0.50 in atomic units at 6 decimals. Override directly with
# STITCHFREN_PRICE_ATOMIC if USD\u20ae0's decimals turn out to differ.
PRICE_ATOMIC = os.getenv("STITCHFREN_PRICE_ATOMIC", "500000")

# payTo/asset now always have a real value (see defaults above) so the 402
# challenge's accepts[] is never null on those fields, regardless of
# Railway env config - this was the root cause of "empty accepts" reports.
# Facilitator creds are still real secrets and CANNOT have safe defaults -
# whether THOSE are set is what facilitator_is_configured() below checks,
# and it's the only thing verify_and_settle now uses to decide fail-open
# vs fail-closed.
FACILITATOR_REQUIRED_ENV = [OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE]

# Explicit, opt-in-only escape hatch for local dev when facilitator creds
# aren't set. Defaults OFF. This existing outside of an explicit "true" is
# what let a paid tool call through for free in production - see
# verify_and_settle's docstring for exactly how.
ALLOW_UNPAID_MCP = os.getenv("STITCHFREN_ALLOW_UNPAID_MCP", "false").strip().lower() == "true"


def facilitator_is_configured() -> bool:
    return all(FACILITATOR_REQUIRED_ENV)


def payment_is_configured() -> bool:
    # Kept for anything still importing the old name; means the same thing
    # facilitator_is_configured() does now that payTo/asset always have
    # values.
    return facilitator_is_configured()


def sign_okx(method: str, request_path: str, body: str = "") -> Dict[str, str]:
    """
    Same prehash formula as server.js's signOkx(): timestamp + method +
    requestPath + body, HMAC-SHA256, base64. Kept in sync with that file -
    if you change one, change both.

    request_path MUST be the full path OKX signs against, e.g.
    "/api/v6/pay/x402/verify" - NOT just "/verify". OKX's own API-signing
    docs (web3.okx.com/onchainos/dev-docs/home/api-access-and-usage) and
    server.js's own call sites both confirm this; signing a short path here
    produces a signature OKX will reject with 50113 "Invalid signature" on
    every call, since the server checks the signature against the actual
    request path it received.
    """
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    sign = base64.b64encode(
        hmac.new(OKX_SECRET_KEY.encode(), prehash.encode(), hashlib.sha256).digest()
    ).decode()
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
    }
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
    return headers


def build_payment_requirements() -> Dict[str, Any]:
    """
    The single 'accepts' entry describing this tool's price, matching
    OKX's PaymentRequirements schema exactly: scheme, network, amount,
    asset, payTo, maxTimeoutSeconds, extra. Nothing else belongs in this
    object per OKX's docs - resource/description/mimeType live separately
    at the top level of the 402 body (see payment_required_body below),
    not inside each accepts[] entry.
    """
    return {
        "scheme": "exact",
        "network": NETWORK,
        "amount": PRICE_ATOMIC,
        "payTo": PAY_TO_ADDRESS,
        "maxTimeoutSeconds": 60,
        "asset": USDT0_ASSET_ADDRESS,
        "extra": {"name": ASSET_NAME, "version": "1"},
    }


def payment_required_body(resource_url: str, error: str) -> Dict[str, Any]:
    """
    The 402 challenge body. x402Version 2 (OKX's facilitator, not v1) -
    resource is its own top-level object here, per OKX/x402 v2's actual
    shape, not nested inside each accepts[] entry.
    """
    return {
        "x402Version": 2,
        "error": error,
        "resource": {
            "url": resource_url,
            "description": (
                "Draft sloper pattern pieces from body measurements, nest "
                "them onto a fabric roll (true NFP placement), and return "
                "SVGs, a cut-ready DXF, and a cutting sheet with verified "
                "fabric savings."
            ),
            "mimeType": "application/json",
        },
        "accepts": [build_payment_requirements()],
        "extensions": {},
    }


class PaymentError(Exception):
    """Raised for any payment problem; message is safe to show the caller."""


async def _facilitator_call(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    path is the short form, e.g. "/verify" - used only to build the request
    URL. The signature is computed against the FULL path
    (FACILITATOR_PATH_PREFIX + path) since that's what OKX actually
    verifies the signature against; see sign_okx's docstring.
    """
    body = json.dumps(payload)
    full_path = FACILITATOR_PATH_PREFIX + path
    headers = sign_okx("POST", full_path, body)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(FACILITATOR_BASE + full_path, content=body, headers=headers)
    if resp.status_code >= 400:
        raise PaymentError(f"Facilitator call to {path} failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    # OKX wraps REST responses as {"code": "...", "msg": "...", "data": {...}}
    # (see server.js's installFacilitatorEnvelopeUnwrap) - unwrap the same way.
    # Per OKX's own docs: "On business errors, code is non-'0' and data is
    # null" - so this MUST check code before trusting data, or a business
    # error (bad signature, unsupported chain, etc) returns None here and
    # the caller crashes on .get() with an opaque AttributeError instead of
    # a clear PaymentError naming what actually went wrong.
    if isinstance(data, dict) and "code" in data:
        if str(data.get("code")) != "0":
            raise PaymentError(f"Facilitator error on {path}: {data.get('msg') or data.get('code')}")
        return data.get("data") or {}
    return data


async def verify_and_settle(payment_header: str, resource_url: str) -> None:
    """
    Raises PaymentError if the payment is missing, malformed, or rejected.
    Returns normally (no return value) once verified and settled.

    payment_header: the raw value from either the PAYMENT-SIGNATURE header
    (OKX's official SDK) or X-PAYMENT (generic x402 spec) - the caller
    (X402Gate in server.py) checks both and passes whichever it found.
    """
    if not facilitator_is_configured():
        if ALLOW_UNPAID_MCP:
            # Explicit opt-in only (STITCHFREN_ALLOW_UNPAID_MCP=true) - for
            # local dev with no OKX creds at hand. Never set this on the
            # deploy OKX's listing points at.
            return
        # FAILS CLOSED. This used to `return` silently here whenever
        # facilitator creds were missing - which meant a Railway deploy
        # with those env vars unset would deliver the paid result to EVERY
        # caller for free, with no error and no on-chain settlement. That
        # is exactly the "deliverable returned, wallet balance unchanged"
        # report. Refusing outright is the correct behavior for a resource
        # that's supposed to be sold, not given away by misconfiguration.
        raise PaymentError(
            "Payment processing is not configured on this deployment "
            "(missing OKX facilitator credentials) - refusing to deliver "
            "a paid result without confirmed on-chain settlement."
        )

    requirements = build_payment_requirements()

    try:
        payload = json.loads(base64.b64decode(payment_header))
    except Exception as exc:
        raise PaymentError(f"Malformed payment header: {exc}") from exc

    verify_result = await _facilitator_call(
        "/verify", {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements}
    )
    if not verify_result.get("isValid", False):
        raise PaymentError(verify_result.get("invalidReason") or "Payment verification failed.")

    settle_result = await _facilitator_call(
        "/settle", {"x402Version": 2, "paymentPayload": payload, "paymentRequirements": requirements}
    )
    if not settle_result.get("success", False):
        raise PaymentError(settle_result.get("errorReason") or "Payment settlement failed.")
