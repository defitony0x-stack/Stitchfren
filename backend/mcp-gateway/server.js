/**
 * Stitchfren MCP Gateway
 * ----------------------
 * A2MCP (pay-per-call) front door for the OKX AI Marketplace.
 *
 * What this is: a thin Node/Express service that sits in front of your
 * existing FastAPI backend (unchanged — nothing in /cutline is touched).
 * It exposes Stitchfren's one real job — draft + nest + export — as a
 * single x402-gated HTTP route, using OKX's x402 facilitator on X Layer
 * (eip155:196), the same network/scheme OKX AI Marketplace settles A2MCP
 * calls on.
 *
 * The payment plumbing (OK-ACCESS-* signing, facilitator client, envelope
 * unwrap) is carried over from your reference x402.js almost verbatim —
 * that part is genuinely reusable. What changed: the priced routes now
 * match Stitchfren's actual skill (draft-and-nest), not the unrelated
 * health-notes routes from the other project.
 *
 * WHAT YOU STILL NEED TO DO BEFORE THIS GOES LIVE:
 *  1. `npm install` — the @x402/* package versions in package.json are
 *     placeholders; check npm for current versions before installing.
 *  2. Get OKX_API_KEY / OKX_SECRET_KEY / OKX_PASSPHRASE by registering as
 *     an Agent Service Provider (ASP) on OKX AI and choosing A2MCP mode —
 *     that registration happens on OKX's side (Onchain OS skill install +
 *     ASP signup), not in this code.
 *  3. Set PAY_TO_ADDRESS to the wallet that should receive settlement.
 *  4. Set STITCHFREN_API_BASE to your deployed FastAPI URL, and
 *     STITCHFREN_SERVICE_KEY to an API key generated once via that
 *     backend's POST /api/keys/generate (this is a *service* key the
 *     gateway uses server-to-server — it is not the same as the
 *     browser-demo key the frontend generates for itself).
 *  5. Confirm the exact OKX facilitator auth-header contract before
 *     trusting this in production — the signOkx() shape below is carried
 *     over unverified from the reference file, same caveat as before: do
 *     one real paid call in staging first.
 *  6. Tune PRICE below — it's a placeholder.
 */

import express from "express";
import crypto from "crypto";

const NETWORK = "eip155:196"; // X Layer — same chain OKX AI settles A2MCP on
const FACILITATOR_URL = "https://web3.okx.com/api/v6/pay/x402";

const REQUIRED_ENV = ["OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE", "PAY_TO_ADDRESS"];

function paymentIsConfigured() {
  return REQUIRED_ENV.every((k) => !!process.env[k]);
}

// Placeholder pricing for the one real skill this backend offers end to
// end (draft + nest + DXF export + cutting sheet, all in one job — see
// app/workers/tasks.py in the backend, which always runs these together).
export const PRICE = process.env.STITCHFREN_PRICE || "$0.50";

// Verified against OKX's published Onchain OS x402 API reference
// (web3.okx.com/onchainos/dev-docs/payments/api-http-onetime, fetched
// 2026-07-27). Prehash formula (timestamp+method+requestPath+body,
// HMAC-SHA256, base64) and the four auth headers match exactly.
// Content-Type is listed as a required header for POST requests in
// OKX's own auth table and was missing before - added below.
function signOkx(method, requestPath, body = "") {
  const timestamp = new Date().toISOString();
  const prehash = `${timestamp}${method.toUpperCase()}${requestPath}${body}`;
  const sign = crypto.createHmac("sha256", process.env.OKX_SECRET_KEY).update(prehash).digest("base64");
  const headers = {
    "OK-ACCESS-KEY": process.env.OKX_API_KEY,
    "OK-ACCESS-SIGN": sign,
    "OK-ACCESS-TIMESTAMP": timestamp,
    "OK-ACCESS-PASSPHRASE": process.env.OKX_PASSPHRASE,
  };
  if (method.toUpperCase() === "POST") {
    headers["Content-Type"] = "application/json";
  }
  return headers;
}

// OKX wraps every REST response (including the x402 facilitator) in
// {"code":"0","msg":"...","data":{...}} - code is a string per OKX's
// docs, not an int as the old comment here said, though the unwrap
// check below is type-agnostic so this never actually broke anything.
// @x402/core's facilitator client expects the unwrapped payload.
// Scoped narrowly to facilitator-URL calls only.
function installFacilitatorEnvelopeUnwrap() {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (input, init) => {
    const url = typeof input === "string" ? input : input?.url;
    const response = await originalFetch(input, init);
    if (!url || !url.startsWith(FACILITATOR_URL)) return response;

    let body;
    try {
      body = await response.clone().json();
    } catch {
      return response;
    }
    if (body && typeof body === "object" && "code" in body && "data" in body) {
      return new Response(JSON.stringify(body.data), {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    }
    return response;
  };
}

async function buildPaymentMiddleware() {
  if (!paymentIsConfigured()) return null;

  const { paymentMiddleware, x402ResourceServer } = await import("@x402/express");
  const { ExactEvmScheme } = await import("@x402/evm/exact/server");
  const { HTTPFacilitatorClient } = await import("@x402/core/server");

  installFacilitatorEnvelopeUnwrap();

  const facilitatorClient = new HTTPFacilitatorClient({
    url: FACILITATOR_URL,
    // Only verify/settle/supported: those are the only three methods on
    // @x402/core's FacilitatorClient interface, and the only endpoints
    // OKX documents (plus settle/status, which this SDK version doesn't
    // call). The old 'list' entry pointed at an endpoint that doesn't
    // exist - /api/v6/pay/x402/list is not in OKX's API reference.
    createAuthHeaders: async () => ({
      verify: signOkx("POST", "/api/v6/pay/x402/verify"),
      settle: signOkx("POST", "/api/v6/pay/x402/settle"),
      supported: signOkx("GET", "/api/v6/pay/x402/supported"),
    }),
  });

  const resourceServer = new x402ResourceServer(facilitatorClient);
  resourceServer.register(NETWORK, new ExactEvmScheme());

  const routes = {
    "POST /mcp/draft-and-nest": {
      accepts: [{ scheme: "exact", network: NETWORK, payTo: process.env.PAY_TO_ADDRESS, price: PRICE }],
      description:
        "Draft sloper pattern pieces from body measurements, nest them onto a fabric roll (true NFP placement), and return SVGs, a cut-ready DXF, and a cutting sheet with verified fabric savings.",
      mimeType: "application/json",
    },
  };

  return paymentMiddleware(routes, resourceServer);
}

// ---------- Backend bridge ----------

const API_BASE = process.env.STITCHFREN_API_BASE; // e.g. https://your-app.up.railway.app
const SERVICE_KEY = process.env.STITCHFREN_SERVICE_KEY;

async function submitJob(payload) {
  const res = await fetch(`${API_BASE}/api/pattern`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-API-Key": SERVICE_KEY },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Backend rejected the job (${res.status}): ${await res.text()}`);
  return res.json(); // { task_id, ... }
}

async function pollJob(taskId, { maxWaitMs = 60_000, intervalMs = 1500 } = {}) {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    const res = await fetch(`${API_BASE}/api/status/${taskId}`, {
      headers: { "X-API-Key": SERVICE_KEY },
    });
    if (!res.ok) throw new Error(`Status check failed (${res.status})`);
    const data = await res.json();
    if (data.status === "completed") return data.result;
    if (data.status === "failed") throw new Error(data.error || "Job failed");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
  return null; // caller falls back to returning the task_id for later polling
}

// ---------- Agent-facing response shaping ----------
//
// An agent reading this in an OKX terminal shouldn't have to parse SVG
// blobs and nested-placement geometry to find out what happened. Every
// completed job gets a short plain-language "message" (the backend's own
// LLM-written narrative when available — see cutting_sheet.narrative,
// generated by app/services/llm_service.py — falling back to a
// templated one built from the same facts if the backend's LLM isn't
// configured or the call failed) plus the DXF download link, both
// front and center. The full structured result still rides along
// underneath for agents that want to parse it programmatically.
function fallbackDirection(result) {
  const sheet = result.cutting_sheet || {};
  const pieceCount = Array.isArray(sheet.pieces) ? sheet.pieces.length : null;
  const parts = [];
  if (sheet.style) parts.push(`Your ${sheet.style.replace(/_/g, " ")} pattern is ready.`);
  if (pieceCount) parts.push(`Cut ${pieceCount} piece${pieceCount === 1 ? "" : "s"}.`);
  if (sheet.fabric_length_needed_cm) parts.push(`Uses ${sheet.fabric_length_needed_cm}cm of fabric.`);
  if (result.fabric_saved_cm > 0) {
    parts.push(`That's ${result.fabric_saved_cm}cm (${result.fabric_saved_pct}%) less than a naive layout.`);
  }
  if (Array.isArray(sheet.notes) && sheet.notes.length) parts.push(sheet.notes.join(" "));
  return parts.join(" ") || "Your pattern and nested cutting layout are ready.";
}

function buildAgentResponse(result, taskId) {
  const direction = (result.cutting_sheet && result.cutting_sheet.narrative) || fallbackDirection(result);
  return {
    ok: true,
    task_id: taskId,
    message: direction,
    download_url: result.dxf_url || null,
    fabric_saved_cm: result.fabric_saved_cm,
    fabric_saved_pct: result.fabric_saved_pct,
    warnings: result.warnings || [],
    cutting_sheet: result.cutting_sheet,
    result,
  };
}



const app = express();
app.use(express.json());

app.get("/health", (_req, res) => {
  res.json({
    status: "ok",
    payment_configured: paymentIsConfigured(),
    backend_configured: !!(API_BASE && SERVICE_KEY),
  });
});

// Free — lets a paying agent check on a job it already submitted without
// paying again.
app.get("/mcp/status/:taskId", async (req, res) => {
  try {
    const result = await pollJob(req.params.taskId, { maxWaitMs: 1 }); // one check, no wait
    res.json(result ? { status: "completed", ...buildAgentResponse(result, req.params.taskId) } : { status: "processing", task_id: req.params.taskId });
  } catch (err) {
    res.status(502).json({ error: err.message });
  }
});

const paymentMiddleware = await buildPaymentMiddleware();
if (!paymentMiddleware) {
  console.warn(
    "[stitchfren-mcp-gateway] OKX payment env vars not set — /mcp/draft-and-nest is running WITHOUT payment gating. Do not point OKX's A2MCP listing at this instance until OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, and PAY_TO_ADDRESS are configured."
  );
} else {
  app.use(paymentMiddleware);
}

app.post("/mcp/draft-and-nest", async (req, res) => {
  if (!API_BASE || !SERVICE_KEY) {
    return res.status(500).json({ error: "Gateway misconfigured: STITCHFREN_API_BASE / STITCHFREN_SERVICE_KEY not set." });
  }
  try {
    const { task_id } = await submitJob(req.body);
    const result = await pollJob(task_id);
    if (result) return res.json(buildAgentResponse(result, task_id));
    // Ran long — payment already settled, so hand back the task_id rather
    // than making the caller pay again; they can poll /mcp/status/:taskId.
    return res.json({
      ok: true,
      pending: true,
      task_id,
      message: "Still crunching the nesting layout — check back in a moment at /mcp/status/" + task_id + ".",
      download_url: null,
    });
  } catch (err) {
    res.status(502).json({ ok: false, error: err.message });
  }
});

const PORT = process.env.PORT || 8402;
app.listen(PORT, () => console.log(`stitchfren-mcp-gateway listening on :${PORT}`));
