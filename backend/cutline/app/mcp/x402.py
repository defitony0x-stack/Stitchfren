"""
x402 payment gate for Stitchfren's MCP tool - SDK-first version.

The previous version of this file called the OKX facilitator client's
.verify()/.settle() directly and hand-built the 402 challenge body
(payment_required_body()) and the PAYMENT-REQUIRED header by hand. That was
real SDK usage in the sense that OKXFacilitatorClient did the signing and the
HTTP calls, but it wasn't the pattern OKX's own quickstart shows. The Python
sample on web3.okx.com/onchainos/dev-docs/payments/service-seller-sdk runs
the whole flow through x402.http.middleware.fastapi.PaymentMiddlewareASGI:
that class owns the 402 response, the payment-header parsing, and the
verify/settle calls. Nothing in this codebase used that class before, and
that's the specific thing OKX's listing rejection ("service isn't integrated
with the official OKX Payment SDK") points at.

This file now builds that middleware and exposes build_paid_app(), which
returns a ready-to-mount ASGI app, instead of reimplementing what the
middleware does. What still can't move into the SDK's model, on any
language's SDK, per the same doc page: PaymentMiddlewareASGI prices one
fixed "METHOD /path" route, and this service's MCP surface is a single
POST /mcp multiplexing several JSON-RPC methods where only tools/call (for
the paid tool) should cost money. server.py routes requests to either the
plain app or this SDK-middleware-wrapped app depending on the JSON-RPC
method in the body - that dispatch has no SDK equivalent, so it stays custom.

UNVERIFIED, flagged rather than guessed at - same discipline as the old
PRICE_ATOMIC decimals flag in this file's previous version:
  - PaymentMiddlewareASGI's constructor signature and RouteConfig/
    PaymentOption's exact field names are taken from the one quickstart code
    sample on that doc page, not from reading the installed package's
    source. I don't have network access in this environment to
    `pip install okxweb3-app-x402` and confirm them directly.
  - Whether RouteConfig/PaymentOption need an explicit `resource` URL field
    (distinct from the route path key) isn't shown in the sample - left out
    below on the assumption the middleware derives it from the request,
    since the doc's own example doesn't pass one.
Do one real paid call against this in staging before trusting it in
production, and if PaymentMiddlewareASGI's constructor rejects any of these
kwargs, paste the TypeError back - it'll name the actual accepted signature.
"""

from __future__ import annotations

import os
from typing import Optional

from x402.http import (
    OKXAuthConfig,
    OKXFacilitatorClient,
    OKXFacilitatorConfig,
    PaymentOption,
)
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact.server import ExactEvmScheme
from x402.server import x402ResourceServer

NETWORK = "eip155:196"  # X Layer, same chain the Node gateway settles on

PAY_TO_ADDRESS = os.getenv("PAY_TO_ADDRESS", "0xac27574ce229cbe4f6a48d7195566dd32f3a1fbb")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
OKX_BASE_URL = os.getenv("OKX_BASE_URL", "https://web3.okx.com")

# USD string, per the doc's own price format ("$0.1"-style) - the middleware
# converts this to atomic units itself via the scheme registered below. No
# more hand-called parse_price() or guessed-at AssetAmount field names.
PRICE_USD = os.getenv("STITCHFREN_PRICE_USD", "$0.50")

FACILITATOR_REQUIRED_ENV = [OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE]

# Explicit, opt-in-only escape hatch for local dev when facilitator creds
# aren't set. Defaults OFF. Never set this on the deploy OKX's listing
# points at - see build_paid_app()'s fail-closed behavior below for why.
ALLOW_UNPAID_MCP = os.getenv("STITCHFREN_ALLOW_UNPAID_MCP", "false").strip().lower() == "true"


def facilitator_is_configured() -> bool:
    return all(FACILITATOR_REQUIRED_ENV)


class PaymentConfigError(Exception):
    """Raised at paid-app build time if facilitator creds are missing and
    the explicit local-dev opt-out isn't set. Message is safe to log."""


_facilitator: Optional[OKXFacilitatorClient] = None
_resource_server: Optional[x402ResourceServer] = None


def _get_resource_server() -> x402ResourceServer:
    """
    Lazy singleton - import-time (tests, or before env vars are set)
    shouldn't crash just because nothing's called yet. Same reasoning the
    old module-level PRICE_ATOMIC/facilitator_is_configured() pairing used.
    """
    global _facilitator, _resource_server
    if _resource_server is None:
        _facilitator = OKXFacilitatorClient(
            OKXFacilitatorConfig(
                auth=OKXAuthConfig(
                    api_key=OKX_API_KEY or "",
                    secret_key=OKX_SECRET_KEY or "",
                    passphrase=OKX_PASSPHRASE or "",
                ),
                base_url=OKX_BASE_URL,
                sync_settle=True,
            )
        )
        _resource_server = x402ResourceServer(_facilitator)
        _resource_server.register(NETWORK, ExactEvmScheme())
    return _resource_server


def _payment_routes(resource_description: str) -> dict:
    """
    One route, matching the doc's RouteConfig/PaymentOption shape. Keyed
    "POST /" rather than "POST /mcp": the app this gets attached to
    (build_paid_app's return value) is reached through server.py's
    dispatcher, which is itself mounted at /mcp by app/api/main.py's
    app.mount("/mcp", mcp_app_gated) - by the time a request reaches this
    inner app, the /mcp prefix has already been stripped from the scope
    path by that outer mount.
    """
    return {
        "POST /": RouteConfig(
            accepts=[
                PaymentOption(
                    scheme="exact",
                    price=PRICE_USD,
                    network=NETWORK,
                    pay_to=PAY_TO_ADDRESS,
                    max_timeout_seconds=60,
                ),
            ],
            description=resource_description,
            mime_type="application/json",
        ),
    }


def build_paid_app(inner_app, resource_description: str):
    """
    Wraps `inner_app` in the real PaymentMiddlewareASGI, wired the way the
    doc's FastAPI example shows: routes={"POST /": RouteConfig(accepts=
    [PaymentOption(...)])}, server=x402ResourceServer with ExactEvmScheme
    registered for eip155:196. The middleware now owns the 402 body, the
    PAYMENT-REQUIRED header, payment-header parsing, and the verify()/
    settle() calls - none of that is hand-rolled in this file anymore.

    Fails closed: if facilitator creds aren't set and the explicit
    local-dev opt-out (STITCHFREN_ALLOW_UNPAID_MCP=true) isn't either,
    raises PaymentConfigError rather than silently building an app that
    would deliver the paid result for free. Same fail-closed reasoning the
    old verify_and_settle() used, just enforced at app-build time instead
    of per-request, since the middleware now owns the per-request path.
    """
    if not facilitator_is_configured():
        if ALLOW_UNPAID_MCP:
            # Explicit opt-in only, for local dev with no OKX creds at
            # hand. Never set this on the deploy OKX's listing points at.
            return inner_app
        raise PaymentConfigError(
            "Payment processing is not configured on this deployment "
            "(missing OKX facilitator credentials) - refusing to build the "
            "paid MCP path without confirmed on-chain settlement capability."
        )

    server = _get_resource_server()
    routes = _payment_routes(resource_description)
    return PaymentMiddlewareASGI(inner_app, routes=routes, server=server)
