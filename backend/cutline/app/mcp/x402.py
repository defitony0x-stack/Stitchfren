"""
x402 payment gate for Stitchfren's MCP tool.

Same OKX facilitator this project's Node gateway (backend/mcp-gateway) talks
to, ported to Python so the paid tool call and the pattern-drafting code it
protects can live in one process/one deploy instead of two. The x402 JSON
shapes below (402 challenge body, X-PAYMENT header, /verify + /settle calls)
follow the public x402 v1 spec (github.com/coinbase/x402/blob/main/specs/
x402-specification.md); the OKX-specific parts (facilitator URL, OK-ACCESS-*
signing) are carried over from backend/mcp-gateway/server.js's signOkx().

STILL UNVERIFIED - same caveat as the Node gateway had:
  - The exact /verify and /settle request/response envelope OKX's
    facilitator expects hasn't been confirmed against a real call. Do one
    real paid call in staging before trusting this live.
  - PRICE_ATOMIC assumes USD\u20ae0 uses 6 decimals (matching USDC/USDT
    convention) - confirm against OKX's own USD\u20ae0 contract decimals
    before relying on the $0.50 conversion below.
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
FACILITATOR_URL = "https://web3.okx.com/api/v6/pay/x402"
ASSET_NAME = "USD\u20ae0"

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS")
USDT0_ASSET_ADDRESS = os.getenv("USDT0_ASSET_ADDRESS")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")

# $0.50 in atomic units at 6 decimals. Override directly with
# STITCHFREN_PRICE_ATOMIC if USD\u20ae0's decimals turn out to differ.
PRICE_ATOMIC = os.getenv("STITCHFREN_PRICE_ATOMIC", "500000")

REQUIRED_ENV = [PAY_TO_ADDRESS, USDT0_ASSET_ADDRESS, OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE]


def payment_is_configured() -> bool:
    return all(REQUIRED_ENV)


def sign_okx(method: str, request_path: str, body: str = "") -> Dict[str, str]:
    """Same prehash formula as server.js's signOkx(): timestamp + method +
    requestPath + body, HMAC-SHA256, base64. Kept in sync with that file -
    if you change one, change both."""
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


def build_payment_requirements(resource_url: str) -> Dict[str, Any]:
    """The single 'accepts' entry describing this tool's price, per x402 v1."""
    return {
        "scheme": "exact",
        "network": NETWORK,
        "maxAmountRequired": PRICE_ATOMIC,
        "resource": resource_url,
        "description": (
            "Draft sloper pattern pieces from body measurements, nest them "
            "onto a fabric roll (true NFP placement), and return SVGs, a "
            "cut-ready DXF, and a cutting sheet with verified fabric savings."
        ),
        "mimeType": "application/json",
        "payTo": PAY_TO_ADDRESS,
        "maxTimeoutSeconds": 60,
        "asset": USDT0_ASSET_ADDRESS,
        "extra": {"name": ASSET_NAME, "version": "1"},
    }


def payment_required_body(resource_url: str, error: str) -> Dict[str, Any]:
    return {
        "x402Version": 1,
        "error": error,
        "accepts": [build_payment_requirements(resource_url)],
    }


class PaymentError(Exception):
    """Raised for any payment problem; message is safe to show the caller."""


async def _facilitator_call(path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    body = json.dumps(payload)
    headers = sign_okx("POST", path, body)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(FACILITATOR_URL + path, content=body, headers=headers)
    if resp.status_code >= 400:
        raise PaymentError(f"Facilitator call to {path} failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    # OKX wraps REST responses as {"code": "...", "msg": "...", "data": {...}}
    # (see server.js's installFacilitatorEnvelopeUnwrap) - unwrap the same way.
    if isinstance(data, dict) and "code" in data and "data" in data:
        return data["data"]
    return data


async def verify_and_settle(x_payment_header: str, resource_url: str) -> None:
    """
    Raises PaymentError if the payment is missing, malformed, or rejected.
    Returns normally (no return value) once verified and settled.
    """
    if not payment_is_configured():
        # Payment env vars aren't set - same "unlocked for local testing"
        # behavior the Node gateway has; do not point OKX's real listing at
        # a deploy running this way.
        return

    requirements = build_payment_requirements(resource_url)

    try:
        payload = json.loads(base64.b64decode(x_payment_header))
    except Exception as exc:
        raise PaymentError(f"Malformed X-PAYMENT header: {exc}") from exc

    verify_result = await _facilitator_call(
        "/verify", {"paymentPayload": payload, "paymentRequirements": requirements}
    )
    if not verify_result.get("isValid", False):
        raise PaymentError(verify_result.get("invalidReason") or "Payment verification failed.")

    settle_result = await _facilitator_call(
        "/settle", {"paymentPayload": payload, "paymentRequirements": requirements}
    )
    if not settle_result.get("success", False):
        raise PaymentError(settle_result.get("error") or "Payment settlement failed.")
