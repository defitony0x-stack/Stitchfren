"""
In-memory ASGI test for the X402Gate fix (no network, no deploy).

Simulates how OKX's x402 endpoint validator and a real buyer agent hit the
/mcp endpoint, and asserts the gate behaves correctly:

  probe (sessionless, non-initialize)  -> 402 with payment-required header
  initialize (sessionless)             -> passes through to fastmcp (200-ish)
  paid tools/call w/o X-PAYMENT        -> 402
  paid tools/call w/ X-PAYMENT (no env)-> passes through (verify no-op)

We import the gate logic WITHOUT the heavy `app` dependency tree by
registering lightweight stub modules for the bits server.py imports at module
load time. The gate only relies on `payment_required_body` from x402, which we
stub to emit a minimal-but-valid challenge.
"""
import asyncio
import base64
import json
import os
import sys
import types

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _stub(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


# Stub the heavy real modules so importing server.py doesn't pull shapely etc.
_stub("app")
_stub("app.mcp")
app_job = _stub("app.mcp.job")
for _n in ("run_pattern_job", "fallback_direction"):
    setattr(app_job, _n, lambda *a, **k: {})
app_models = _stub("app.models")
app_schemas = _stub("app.models.schemas")
# server.py does `from app.models.schemas import Measurements, PatternRequest, PatternStyle`
for _n in ("Measurements", "PatternRequest", "PatternStyle"):
    setattr(app_schemas, _n, object)


# Minimal x402 stub: only what server.py calls at import + runtime.
x402 = _stub("app.mcp.x402")


class PaymentError(Exception):
    pass


def payment_required_body(resource_url, error):
    return {
        "x402Version": 2,
        "error": error,
        "resource": {"url": resource_url, "description": "Stitchfren probe", "mimeType": "application/json"},
        "accepts": [{
            "scheme": "exact",
            "network": "eip155:196",
            "amount": "500000",
            "asset": "0x779dEd0c9E1022225F8e0630b35a9b54BE713736",
            "payTo": "0xac27574ce229cbe4f6a48d7195566dd32f3a1fbb",
            "maxTimeoutSeconds": 60,
            "extra": {"name": "USDT0", "version": "1"},
        }],
        "extensions": {},
    }


async def verify_and_settle(x_payment_header, resource_url):
    # With no real facilitator env configured, behave like the real code:
    # verify_and_settle returns (no-op) when payment env isn't configured.
    return None


x402.PaymentError = PaymentError
x402.payment_required_body = payment_required_body
x402.verify_and_settle = verify_and_settle
app_mcp = _stub("app.mcp")
app_mcp.x402 = x402

# Now import the gate module itself (the real server.py) and grab its gate,
# replacing only the fastmcp-backed inner app with a passthrough that records
# pass-through.
sys.path.insert(0, REPO)
import importlib.util

spec = importlib.util.spec_from_file_location("gate_server", os.path.join(REPO, "backend", "cutline", "app", "mcp", "server.py"))
srv = importlib.util.module_from_spec(spec)
# Provide a fake fastmcp so `from fastmcp import FastMCP` works without install.
fm = types.ModuleType("fastmcp")


class FakeFastMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, func):
        return func

    def http_app(self, path="/"):
        async def fake_app(scope, receive, send):
            # fake fastmcp: echo a 200 + initialize-style result for pass-through
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": json.dumps({"ok": True, "name": self.name}).encode()})
        return fake_app


fm.FastMCP = FakeFastMCP
sys.modules["fastmcp"] = fm

spec.loader.exec_module(srv)
# The gate wraps a fake inner app instead of the real fastmcp server.
srv.mcp_app = fm.FastMCP("Stitchfren").http_app()
srv.mcp_app_gated = srv.X402Gate(srv.AcceptFixer(srv.mcp_app))


def make_scope(method="POST", path="/mcp/", headers=None, body=b""):
    hdrs = [(b"host", b"stitchfren-production.up.railway.app")]
    for k, v in (headers or {}).items():
        hdrs.append((k.encode(), v.encode()))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "scheme": "https",
        "headers": hdrs,
    }, body


async def call(app, method="POST", path="/mcp/", headers=None, body=b""):
    scope, body = make_scope(method, path, headers, body)
    captured = {"status": None, "headers": [], "body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            captured["status"] = msg["status"]
            captured["headers"] = msg.get("headers", [])
        elif msg["type"] == "http.response.body":
            captured["body"] += msg.get("body", b"")

    await app(scope, receive, send)
    return captured


def decode_challenge(captured):
    for k, v in captured["headers"]:
        if k.lower() == b"payment-required":
            return json.loads(base64.b64decode(v))
    return None


async def main():
    results = []

    # 1) OKX validator probe: sessionless tools/list -> MUST be 402 now
    r = await call(
        srv.mcp_app_gated,
        headers={"accept": "application/json"},
        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
    )
    ch = decode_challenge(r)
    results.append(("probe sessionless tools/list", r["status"], bool(ch),
                    ch.get("x402Version") if ch else None,
                    (ch["accepts"][0].get("scheme") if ch and ch.get("accepts") else None)))

    # 2) initialize (sessionless) -> must pass through (NOT 402)
    r = await call(
        srv.mcp_app_gated,
        headers={"accept": "application/json"},
        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}).encode(),
    )
    results.append(("initialize sessionless", r["status"], decode_challenge(r) is not None, None, None))

    # 3) paid tools/call without X-PAYMENT -> 402
    r = await call(
        srv.mcp_app_gated,
        headers={"accept": "application/json", "mcp-session-id": "sess123"},
        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "draft_and_nest_pattern",
                                    "arguments": {"style": "bodice_aline", "bust_or_chest": 92,
                                                  "waist": 74, "fabric_width_cm": 150}}}).encode(),
    )
    ch = decode_challenge(r)
    results.append(("paid call no x-payment", r["status"], bool(ch),
                    ch.get("x402Version") if ch else None,
                    (ch["accepts"][0].get("payTo") if ch and ch.get("accepts") else None)))

    # 4) paid tools/call WITH x-payment header (env not set -> verify is no-op) -> passes through (NOT 402)
    r = await call(
        srv.mcp_app_gated,
        headers={"accept": "application/json", "mcp-session-id": "sess123", "x-payment": "dummytoken"},
        body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": "draft_and_nest_pattern",
                                    "arguments": {"style": "bodice_aline", "bust_or_chest": 92,
                                                  "waist": 74, "fabric_width_cm": 150}}}).encode(),
    )
    results.append(("paid call with x-payment", r["status"], decode_challenge(r) is not None, None, None))

    # 5) SSE GET without session -> 402 (probe)
    r = await call(srv.mcp_app_gated, method="GET", headers={}, body=b"")
    results.append(("SSE GET no session", r["status"], decode_challenge(r) is not None, None, None))

    print(f"{'CASE':<34}{'STATUS':<8}{'HAS_402':<9}{'X402VER':<9}EXTRA")
    for name, status, has402, ver, extra in results:
        print(f"{name:<34}{status!s:<8}{str(has402):<9}{str(ver):<9}{extra}")

    # Assertions: the fix's contract
    assert results[0][1] == 402 and results[0][2], "FAIL: sessionless probe must return 402"
    assert not results[1][2], "FAIL: initialize must NOT be gated"
    assert results[2][1] == 402 and results[2][2], "FAIL: unpaid paid-call must return 402"
    assert not results[3][2], "FAIL: paid-call with x-payment must pass through"
    assert results[4][1] == 402 and results[4][2], "FAIL: sessionless SSE GET must return 402"
    print("\nALL GATE ASSERTIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
