# AXL402 — Demo

> Interactive web dashboard for **AXL402**: pay-gated peer-to-peer agent
> infrastructure. Every MCP/A2A call to a gated AXL node settles real
> USDC on-chain (Base Sepolia) via [x402](https://x402.org).

This repo is the demo / dashboard. The Go fork that adds the payment
gate to AXL itself lives separately and is referenced via the
`AXL402_BINARY` env var.

---

## What you see

A single-page dashboard at `http://127.0.0.1:8080` that drives two real
AXL nodes — one ungated (caller), one gated (receiver) — over a real
Yggdrasil mesh, and pays the gated node with real Base Sepolia USDC
through Coinbase's hosted [x402 facilitator](https://x402.org/facilitator).

| Panel | What it shows |
|---|---|
| **Live price chart** | Polls `/x402/price` at 2 Hz. Sparkline goes amber → rose as surge factor climbs. |
| **Network panel** | Both AXL pubkeys, peer counts, wallet status (never the address). |
| **Action buttons** | One click per scenario: stock MCP probe, x402 envelope (no pay), or full pay-and-call with on-chain settlement. |
| **Attack simulator** | Slider 1–40, "launch attack" fires concurrent paid calls. Sparkline spikes, total spent ticks up, peak price displayed. |
| **Event log** | Server-Sent-Event stream of every internal step: probe → 402 → sign → settle → paid. |

Every settled call links straight to its
[Basescan](https://sepolia.basescan.org/) transaction.

---

## Architecture

```
┌──────────────────┐      ┌─────────────────┐      ┌──────────────────┐
│  Browser         │◀────▶│  FastAPI server │◀────▶│  Caller AXL node │
│  (this dashboard)│ HTTP │  (server.py)    │ HTTP │  :9002 (ungated) │
└──────────────────┘      │  · holds signer │      └──────────────────┘
                          │  · proxies AXL  │                │ Yggdrasil
                          │  · SSE events   │                ▼ mesh
                          └─────────────────┘      ┌──────────────────┐
                                  │                │  Gated AXL node  │
                                  │ /verify        │  :9012 (AXL402)  │
                                  │ /settle        │  · gate stream   │
                                  ▼                │  · surge pricing │
                          ┌─────────────────┐      └──────────────────┘
                          │ Coinbase x402   │                │
                          │ facilitator     │                ▼ MCP route
                          │ (Base Sepolia)  │      ┌──────────────────┐
                          └─────────────────┘      │  fake_mcp_router │
                                                   │  :9103           │
                                                   └──────────────────┘
```

The browser only ever talks to the local FastAPI proxy. The proxy holds
the EIP-3009 signer (reads `PRIVATE_KEY` from env, never echoes it) and
the bookkeeping that makes the attack-simulator feasible. The actual
gating is all done by the AXL402 node — `server.py` is a thin demo
shell.

---

## Setup

### 1. Build the AXL402 node binary

Clone the AXL402 fork (the Go work) and build:

```bash
git clone https://github.com/lordshashank/axl    # the AXL402 fork
cd axl
make build                                        # produces ./node
```

The default expectation is that the AXL402 repo lives next to this one:

```
hackathons/
├── axl/                  # AXL402 fork (with the gate stream)
│   └── node              # compiled binary
└── AXL402-demo/          # this repo
```

If it lives elsewhere, point `AXL402_BINARY` at the binary explicitly.

### 2. Fund a Base Sepolia wallet

The demo signs real EIP-3009 `transferWithAuthorization` messages and
asks Coinbase's facilitator to settle on Base Sepolia. The wallet needs
a small amount of **Base Sepolia USDC**:

| Need | Where |
|---|---|
| Base Sepolia ETH (gas) | <https://www.alchemy.com/faucets/base-sepolia> |
| Base Sepolia USDC      | <https://faucet.circle.com> (pick "Base Sepolia") |

A few cents of USDC is enough — each demo call costs `0.01` USDC.

### 3. Run the dashboard

```bash
export PRIVATE_KEY=0x...     # the funded Base Sepolia wallet
./run.sh
```

First run creates a `./.venv` and installs `fastapi`, `eth-account`,
`httpx`. Subsequent runs reuse it. Open <http://127.0.0.1:8080>.

`Ctrl-C` tears down all four child processes.

### Switching between fixed and surge pricing

```bash
AXL402_MODE=fixed ./run.sh    # configs/node-B-fixed.json
AXL402_MODE=surge ./run.sh    # configs/node-B-surge.json   (default)
```

---

## Demo flow (90 seconds)

1. **Idle**. Sparkline flat at base price; mode shown as **SURGE**.
2. Click **stock MCP probe** — the dashboard sends a vanilla MCP
   envelope. The gated node refuses it with a clean JSON-RPC
   `-32402 Payment Required` carrying full PaymentRequirements. No
   client fork was needed to discover this.
3. Click **pay & call** — watch the event log: `probe → 402 → sign →
   settle → paid`. A real transaction lands on Base Sepolia in ~3 s.
   Click the tx hash; Basescan opens.
4. Drag attack slider to **25**, click **launch attack**. Sparkline
   ramps vertically. Surge factor reaches `5–20×`. The "total spent"
   counter shows the attacker has now paid you, not crashed your node.
5. Wait 30 s. Sparkline drifts back to baseline — the recent-paid-rate
   window decays. The system self-recovers without any operator action.

---

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI proxy + signer + SSE event bus |
| `index.html` | Single-page dashboard (Tailwind + Alpine + Chart.js, all CDN, no build) |
| `x402_signer.py` | EIP-3009 typed-data signer; reads `PRIVATE_KEY`, never logs it |
| `fake_mcp_router.py` | Minimal stand-in for an MCP router so the gated node has something to dispatch to |
| `run.sh` | Orchestrator: starts router + nodes + dashboard, tears down on exit |
| `requirements.txt` | Python deps |
| `configs/node-A.json` | Caller AXL config (ungated) |
| `configs/node-B-fixed.json` | Gated AXL config — fixed pricing |
| `configs/node-B-surge.json` | Gated AXL config — quadratic surge pricing |

---

## Privacy note

`PRIVATE_KEY` is read from env once and held in-memory by `server.py`
to sign EIP-3009 messages. It is **never** written to disk, logged, or
returned in any HTTP response. The dashboard reports only "wallet ready"
or "unconfigured" — it does not display the derived address either.

---

## Related

- **AXL402 fork (Go):** the actual gate-stream implementation —
  `internal/x402/` plus the listener/main wiring. Repo:
  <https://github.com/lordshashank/axl>.
- **Upstream AXL:** <https://github.com/gensyn-ai/axl>.
- **x402 spec:** <https://www.x402.org/>, <https://github.com/coinbase/x402>.
