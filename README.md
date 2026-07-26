# Stitchfren

Pattern drafting & fabric-nesting API — measurements in, a cut-ready layout out.
Listed on the OKX AI Marketplace as an A2MCP (pay-per-call) service.

## Layout

```
frontend/                 Marketing site + live demo (stitchfren.html)
backend/cutline/          FastAPI + Celery backend (pattern drafting, nesting, DXF export, LLM cutting-sheet layer)
backend/mcp-gateway/      x402-gated Node service for the OKX A2MCP listing
```

`backend/cutline/` is the backend you already had — untouched.
`frontend/` and `backend/mcp-gateway/` are new and both call into it over
HTTP; neither modifies it. `mcp-gateway` is a separate Node process from
`cutline`'s Python stack — they live under the same `backend/` folder
because both are server-side, but deploy as two separate services.

## How the pieces connect

```
frontend/stitchfren.html  ──▶  backend/cutline (FastAPI)  ◀──  backend/mcp-gateway (Node, x402-gated)
   (human demo, browser)          /api/pattern                   (agents on OKX AI, pay-per-call)
                                   /api/status
```

- **Humans** use the demo on the marketing site. It talks to
  `backend/cutline` directly over its normal REST API.
- **Agents** on the OKX AI Marketplace talk to `backend/mcp-gateway`,
  which charges per call via OKX's x402 facilitator, then forwards the job
  to the same `cutline` backend server-to-server.

## 1. Backend (`backend/cutline/`)

Already built — FastAPI + Celery + Postgres, deployed to Railway. See its
own `.env.example` for DB/R2/LLM config. Nothing here changed.

**LLM note:** an LLM layer already exists
(`backend/cutline/app/services/llm_service.py`) — optional free-text
measurement parsing and a narrative on the cutting sheet, both with
rule-based fallbacks when `LLM_API_KEY` is unset. You don't need to add
one; just set `LLM_API_KEY` (DeepSeek by default, or any OpenAI-compatible
provider) to turn it on.

## 2. Frontend (`frontend/stitchfren.html`)

Static single-file site. The "Live demo" section is wired to call the
backend directly:

1. Open the page and paste your deployed backend URL into the **API
   endpoint** field in the demo section (e.g.
   `https://your-app.up.railway.app`). It's saved to the browser's
   localStorage, so you only do this once per browser.
2. First submit generates a demo API key via `POST /api/keys/generate` and
   stores it locally.
3. Fill in style + measurements, hit **Draft & nest this**. The page
   submits to `/api/pattern`, polls `/api/status/{task_id}`, and renders
   the pattern SVG, nested layout SVG, fabric-savings numbers, cutting
   sheet, and a DXF download link.

No build step — deploy as a static file anywhere (Vercel, Netlify, GitHub
Pages, or served by the backend itself).

## 3. OKX Marketplace gateway (`backend/mcp-gateway/`)

Node/Express service exposing the one real job this backend does —
draft + nest + export + cutting sheet — as a single x402-gated route,
`POST /mcp/draft-and-nest`, for OKX's A2MCP (**a**gent-**t**o-**a**gent,
pay-per-call) listing mode. An agent on OKX AI calls this route directly,
no human in the loop — that's what A2MCP means: one agent paying another
agent for a completed task. Deploys as its own service, separate from
`cutline`'s Python deploy (different runtime, different Railway/host
service).

**Response shape.** Every completed job — whether returned immediately
from `/mcp/draft-and-nest` or fetched later from `/mcp/status/:taskId` —
comes back with a plain-language `message` and a `download_url` up front,
not just raw geometry:

```json
{
  "ok": true,
  "task_id": "...",
  "message": "Your bodice — a-line pattern is ready. Cut 6 pieces using 210cm of fabric, 12cm less than a naive layout...",
  "download_url": "https://.../stitchfren_....dxf",
  "fabric_saved_cm": 12.4,
  "fabric_saved_pct": 5.6,
  "cutting_sheet": { ... },
  "result": { ... }
}
```

`message` is the backend's own LLM-written narrative
(`cutting_sheet.narrative`, from `app/services/llm_service.py`) when
`LLM_API_KEY` is configured on the `cutline` backend; if it isn't (or the
call fails), the gateway falls back to a templated sentence built from the
same facts, so an agent always gets a human-readable direction alongside
the link, never just a bare JSON blob. The full structured `result` still
rides along underneath for agents that want to parse geometry
programmatically.

### Setup

```bash
cd backend/mcp-gateway
npm install
cp .env.example .env   # fill in the values below
npm start
```

| Env var | Where it comes from |
|---|---|
| `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE` | Register as an Agent Service Provider (A2MCP mode) on OKX AI — this happens on OKX's side, not in this repo |
| `PAY_TO_ADDRESS` | Your Agentic Wallet / payout address on X Layer |
| `STITCHFREN_API_BASE` | Your deployed `cutline` backend URL |
| `STITCHFREN_SERVICE_KEY` | An API key generated once via `cutline`'s `POST /api/keys/generate`, used server-to-server by the gateway |
| `STITCHFREN_PRICE` | Optional — overrides the default `$0.50` per job |

Until the OKX env vars are set, the gateway logs a warning and runs
**without payment gating** — fine for local testing, not for pointing
OKX's listing at it.

### Still to do on OKX's side

Registering as an ASP and pointing the marketplace at this gateway's
`/mcp/draft-and-nest` endpoint happens through OKX's own signup flow
(Onchain OS skill install + ASP registration) — not something this repo
can do for you.

## Open items worth deciding before launch

- **Pricing**: `$0.50` per job — you know your margins
  and what competing ASPs charge better than I do.
- **`@x402/*` package versions**: pinned to versions confirmed against npm
  (`@x402/core` 2.3.0, `@x402/evm` 2.9.0). `@x402/express`'s exact current
  number wasn't independently confirmed — pinned to `^2.3.0` as a
  reasonable match to `@x402/core`; double check on npm before relying on
  it.
- **Auth header contract**: `signOkx()` in `backend/mcp-gateway/server.js`
  is carried over from your reference file unverified — do one real paid
  call against OKX's facilitator in staging before trusting it live.
