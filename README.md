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
| **Live price chart** | Polls `/axl402/price` at 2 Hz. Sparkline goes amber → rose as surge factor climbs. |
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
                          └─────────────────┘      │ dummy_mcp_router │
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

The Go work lives on the `axl402-payment-gate` branch of
[`lordshashank/axl`](https://github.com/lordshashank/axl) — that fork's
`main` mirrors upstream, so make sure you check out the feature branch:

```bash
git clone -b axl402-payment-gate https://github.com/lordshashank/axl
cd axl
make build                                        # produces ./node
```

See [`AXL402.md`](https://github.com/lordshashank/axl/blob/axl402-payment-gate/AXL402.md)
in the fork for what's added on top of upstream (gate stream, pricers,
`/axl402/` endpoint, config schema).

The default expectation is that the AXL402 repo lives next to this one:

```
hackathons/
├── axl/                  # AXL402 fork, axl402-payment-gate branch
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

## Migrating from stock AXL

Already running an unpaid AXL node and want to gate inbound traffic
behind real on-chain payments? Three steps. The gate is fully opt-in —
omit the new config keys and the node behaves exactly like upstream.

### 1. Swap the binary

```bash
# stop your current node first

git clone -b axl402-payment-gate https://github.com/lordshashank/axl axl402
cd axl402
make build                # produces ./node, drop-in compatible

# point your existing node-config.json at the new binary, or just
# replace the binary in place — config schema is a strict superset
# of upstream's.
```

If you want the patch only (no fork checkout), the four code commits
on the branch are clean and rebase-friendly:

```
git fetch https://github.com/lordshashank/axl axl402-payment-gate
git cherry-pick <sha1>..<sha4>     # see branch's commit list
```

### 2. Decide what to charge

Pick a stablecoin asset, a recipient address you control, and a price.
For the demo we use USDC on Base Sepolia (testnet, free faucet) at
0.01 USDC per call (`10000` atomic units, since USDC has 6 decimals).

Mainnet equivalents work the same way — switch `x402_network` to
`base` and `x402_asset` to mainnet USDC.

### 3. Add the gating block to `node-config.json`

Append these fields to your existing config. Existing fields stay
untouched; the gate just turns on.

**Fixed-price mode** (predictable, simple):

```jsonc
{
  // ... your existing fields (Peers, Listen, api_port, etc.) ...

  "router_addr": "http://127.0.0.1",            // your MCP router
  "router_port": 9003,

  "x402_facilitator_url": "https://x402.org/facilitator",
  "x402_pay_to":          "0xYourReceivingWallet",
  "x402_asset":           "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  // USDC Base Sepolia
  "x402_network":         "base-sepolia",
  "x402_amount":          "10000",              // 0.01 USDC per call

  "axl402_pricing_mode":  "fixed",
  "axl402_quote_ttl_secs": 60,
  "axl402_quota_ttl_secs": 600
}
```

**Surge mode** (DoS-resistant; price climbs with concurrent load):

```jsonc
{
  // ... shared x402_* fields above ...
  "x402_base_amount":     "10000",              // floor / idle price

  "axl402_pricing_mode":   "surge",
  "axl402_pricing_curve":  "quadratic",          // "linear" | "quadratic" | "sqrt"
  "axl402_pricing_n":      3,                    // load divisor; lower = steeper
  "axl402_max_surge":      100,                  // hard ceiling
  "axl402_quote_ttl_secs": 30,
  "axl402_quota_ttl_secs": 600
}
```

**Restart the node.** When the gate is active you'll see this on stdout:

```
[node] axl402 gate enabled: mode=surge base=10000 asset=0x036C... network=base-sepolia ...
axl402 payment gating ENABLED — gate stream is the sole inbound handler
```

### What changes for callers

| Endpoint | Before | After (gated node) |
|---|---|---|
| `POST /mcp/{peer}/{service}` | served free | replies with JSON-RPC `-32402 Payment Required` until paid |
| `POST /a2a/{peer}` | served free | same — JSON-RPC `-32402` |
| `POST /axl402/{peer}` (NEW) | n/a | unified envelope — preferred path for paying clients |
| `GET  /axl402/price` (NEW) | n/a | live price + surge factor |

**Stock clients keep working** — they just discover the price via the
`-32402` error and either pay (using the `/axl402/{peer}` endpoint
with an EIP-3009-signed payment) or back off. No client fork needed
to learn that payment is required.

For a working caller helper, see
[`x402_signer.py`](./x402_signer.py) — it reads `PRIVATE_KEY` from env,
signs the EIP-3009 typed data, and never logs the key.

### Rollback

Delete the new config keys, restart. Or run the upstream binary —
the new fields are simply ignored by upstream's parser.

---

## Files

| File | Purpose |
|---|---|
| `server.py` | FastAPI proxy + signer + SSE event bus |
| `index.html` | Single-page dashboard (Tailwind + Alpine + Chart.js, all CDN, no build) |
| `x402_signer.py` | EIP-3009 typed-data signer; reads `PRIVATE_KEY`, never logs it |
| `dummy_mcp_router.py` | Minimal stand-in for an MCP router so the gated node has something to dispatch to |
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
  `internal/axl402/` plus the listener/main wiring. Branch:
  [`lordshashank/axl @ axl402-payment-gate`](https://github.com/lordshashank/axl/tree/axl402-payment-gate).
  Fork story: [`AXL402.md`](https://github.com/lordshashank/axl/blob/axl402-payment-gate/AXL402.md).
- **Upstream AXL:** <https://github.com/gensyn-ai/axl>.
- **x402 spec:** <https://www.x402.org/>, <https://github.com/coinbase/x402>.
