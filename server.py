"""
FastAPI backend for the AXL402 demo.

Holds the signer (PRIVATE_KEY env var, never echoes it). Owns the gated
AXL node lifecycle so the dashboard can switch pricing modes and tweak
surge parameters without restarting the whole demo. Proxies AXL endpoints
and exposes:

  GET  /api/state         caller + gated pubkeys, peers, wallet ready
  GET  /api/price         current price snapshot from gated /axl402/price
  GET  /api/wallet        usdc/eth balance of payer wallet (no address!)
  GET  /api/config        current pricing-mode params for the gated node
  POST /api/config        set new params, restart the gated node
  POST /api/call          full probe-and-pay flow (one click)
  POST /api/sign          EIP-3009 typed-data signature (no submit)
  POST /api/submit        submit a complete envelope (with optional payment)
  POST /api/attack        fire N concurrent paid calls
  GET  /api/events        SSE stream of activity
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from x402_signer import sign_payment, address_from_env  # noqa: E402


# ── Locations & defaults ──────────────────────────────────────────────────

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")
CONFIGS_DIR = os.path.join(HERE, "configs")
ACTIVE_CONFIG = os.path.join(CONFIGS_DIR, "_active.json")  # gitignored

CALLER_AXL = os.environ.get("CALLER_AXL", "http://127.0.0.1:9002")
GATED_AXL  = os.environ.get("GATED_AXL",  "http://127.0.0.1:9012")

AXL402_BINARY = os.environ.get("AXL402_BINARY", os.path.join(HERE, "..", "axl", "node"))

# Base Sepolia chain
BASE_SEPOLIA_RPC = os.environ.get("BASE_SEPOLIA_RPC", "https://sepolia.base.org")
USDC_CONTRACT    = os.environ.get("USDC_CONTRACT", "0x036CbD53842c5426634e7929541eC2318f3dCF7e")


# ── Event bus (SSE) ────────────────────────────────────────────────────────

@dataclass
class Event:
    ts: float
    kind: str
    msg: str
    extra: Optional[dict] = None


class EventBus:
    def __init__(self) -> None:
        self.subscribers: list[asyncio.Queue[Event]] = []
        self.history: list[Event] = []

    def emit(self, kind: str, msg: str, extra: Optional[dict] = None) -> None:
        ev = Event(ts=time.time(), kind=kind, msg=msg, extra=extra or {})
        self.history.append(ev)
        if len(self.history) > 300:
            self.history = self.history[-300:]
        for q in self.subscribers:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=200)
        self.subscribers.append(q)
        for ev in self.history[-50:]:
            await q.put(ev)
        try:
            while True:
                yield await q.get()
        finally:
            self.subscribers.remove(q)


bus = EventBus()


# ── Gated node lifecycle ───────────────────────────────────────────────────

DEFAULT_GATED_CONFIG: dict[str, Any] = {
    "Peers": [
        "tls://34.46.48.224:9001",
        "tls://136.111.135.206:9001",
    ],
    "Listen": [],
    "api_port": 9012,
    "tcp_port": 7000,
    "router_addr": "http://127.0.0.1",
    "router_port": 9103,

    "x402_facilitator_url": "https://x402.org/facilitator",
    "x402_pay_to": "0xFe643b54727d53C49835f9f6c1a2B9861E741d98",
    "x402_asset":  "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
    "x402_network": "base-sepolia",
    "axl402_quota_ttl_secs": 600,
    "axl402_quote_ttl_secs": 30,

    "axl402_pricing_mode":  "surge",
    "x402_amount":        "10000",
    "x402_base_amount":   "10000",
    "axl402_pricing_curve": "quadratic",
    "axl402_pricing_n":     3,
    "axl402_max_surge":     100,
}


class GatedNode:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.config: dict[str, Any] = dict(DEFAULT_GATED_CONFIG)

    def write_config(self) -> str:
        os.makedirs(CONFIGS_DIR, exist_ok=True)
        with open(ACTIVE_CONFIG, "w") as f:
            json.dump(self.config, f, indent=2)
        return ACTIVE_CONFIG

    def start(self) -> None:
        if not os.path.isfile(AXL402_BINARY):
            bus.emit("error", f"AXL402 binary not found at {AXL402_BINARY}")
            return
        cfg_path = self.write_config()
        # Stdout/stderr go to a log file rather than the dashboard's stdout.
        log_path = "/tmp/axl-B.log"
        self._log = open(log_path, "ab")
        self.proc = subprocess.Popen(
            [AXL402_BINARY, "-config", cfg_path],
            stdout=self._log, stderr=self._log,
        )
        bus.emit("system", f"gated node started (pid {self.proc.pid}) mode={self.config['axl402_pricing_mode']}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGTERM)
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            except Exception as e:
                bus.emit("error", f"stop gated node: {e}")
        if hasattr(self, "_log"):
            try: self._log.close()
            except Exception: pass
        self.proc = None

    async def restart(self) -> None:
        bus.emit("system", "restarting gated node…")
        self.stop()
        await asyncio.sleep(0.5)
        self.start()
        # Wait for the new node to expose its API
        for _ in range(60):
            try:
                async with httpx.AsyncClient(timeout=2.0) as c:
                    r = await c.get(f"{GATED_AXL}/topology")
                    if r.status_code == 200:
                        bus.emit("system", "gated node up")
                        return
            except Exception:
                pass
            await asyncio.sleep(1)
        bus.emit("error", "gated node failed to come up after restart")


gated_node = GatedNode()


# ── Wallet balance bookkeeping ─────────────────────────────────────────────

class WalletState:
    def __init__(self) -> None:
        self.address: Optional[str] = None
        self.initial_usdc: Optional[int] = None  # atomic units at startup
        try:
            self.address = address_from_env()
        except Exception:
            pass

    @property
    def ready(self) -> bool:
        return self.address is not None


wallet = WalletState()


async def _eth_call(rpc: str, to: str, data: str) -> Optional[str]:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    async with httpx.AsyncClient(timeout=8.0) as c:
        r = await c.post(rpc, json=body)
        j = r.json()
    return j.get("result")


async def _eth_balance(rpc: str, addr: str) -> Optional[int]:
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_getBalance",
        "params": [addr, "latest"],
    }
    async with httpx.AsyncClient(timeout=8.0) as c:
        r = await c.post(rpc, json=body)
        j = r.json()
    if "result" in j:
        return int(j["result"], 16)
    return None


async def fetch_wallet_balances() -> dict[str, Any]:
    """Returns USDC + ETH balances (atomic units / wei). Address is NEVER
    returned — it stays inside the server."""
    if not wallet.ready:
        return {"ready": False}
    addr = wallet.address.lower()
    selector = "0x70a08231"  # balanceOf(address)
    arg = addr.replace("0x", "").rjust(64, "0")
    data = selector + arg

    usdc_hex = await _eth_call(BASE_SEPOLIA_RPC, USDC_CONTRACT, data)
    eth_wei  = await _eth_balance(BASE_SEPOLIA_RPC, addr)
    usdc_atomic = int(usdc_hex, 16) if usdc_hex else None

    out: dict[str, Any] = {
        "ready": True,
        "usdc_atomic": usdc_atomic,
        "eth_wei": eth_wei,
    }
    # Track initial balance once we successfully fetch
    if usdc_atomic is not None:
        if wallet.initial_usdc is None:
            wallet.initial_usdc = usdc_atomic
        out["initial_usdc_atomic"] = wallet.initial_usdc
        out["spent_atomic"] = max(0, wallet.initial_usdc - usdc_atomic)
    return out


# ── App lifecycle ──────────────────────────────────────────────────────────

app = FastAPI()


@app.on_event("startup")
async def _startup() -> None:
    gated_node.start()
    # warm up — caller AXL is started by run.sh, gated is ours
    bus.emit("system", "dashboard ready")


@app.on_event("shutdown")
async def _shutdown() -> None:
    gated_node.stop()


# ── Helpers ────────────────────────────────────────────────────────────────


async def _topology(url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as c:
        r = await c.get(f"{url}/topology")
        r.raise_for_status()
        return r.json()


async def _gated_pubkey() -> str:
    t = await _topology(GATED_AXL)
    return t["our_public_key"]


# ── API ────────────────────────────────────────────────────────────────────


@app.get("/api/state")
async def state() -> dict[str, Any]:
    out: dict[str, Any] = {"caller": None, "gated": None, "wallet": None}
    try:
        c = await _topology(CALLER_AXL)
        out["caller"] = {"pubkey": c["our_public_key"], "ipv6": c["our_ipv6"], "peers": len(c.get("peers", []))}
    except Exception as e:
        out["caller"] = {"error": str(e)}
    try:
        g = await _topology(GATED_AXL)
        out["gated"] = {"pubkey": g["our_public_key"], "ipv6": g["our_ipv6"], "peers": len(g.get("peers", []))}
    except Exception as e:
        out["gated"] = {"error": str(e)}
    out["wallet"] = {"ready": wallet.ready}
    return out


@app.get("/api/price")
async def price() -> Any:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{GATED_AXL}/axl402/price")
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/wallet")
async def wallet_endpoint() -> Any:
    try:
        return await fetch_wallet_balances()
    except Exception as e:
        return JSONResponse({"ready": wallet.ready, "error": str(e)}, status_code=200)


@app.get("/api/config")
async def get_config() -> dict[str, Any]:
    cfg = gated_node.config
    return {
        "pricing_mode":   cfg.get("axl402_pricing_mode"),
        "amount":         cfg.get("x402_amount"),
        "base_amount":    cfg.get("x402_base_amount"),
        "pricing_curve":  cfg.get("axl402_pricing_curve"),
        "pricing_n":      cfg.get("axl402_pricing_n"),
        "max_surge":      cfg.get("axl402_max_surge"),
        "quote_ttl_secs": cfg.get("axl402_quote_ttl_secs"),
        "quota_ttl_secs": cfg.get("axl402_quota_ttl_secs"),
        "pay_to":         cfg.get("x402_pay_to"),
        "asset":          cfg.get("x402_asset"),
        "network":        cfg.get("x402_network"),
    }


class ConfigReq(BaseModel):
    pricing_mode: Optional[str] = None
    amount: Optional[str] = None
    base_amount: Optional[str] = None
    pricing_curve: Optional[str] = None
    pricing_n: Optional[int] = None
    max_surge: Optional[float] = None
    quote_ttl_secs: Optional[int] = None
    quota_ttl_secs: Optional[int] = None


@app.post("/api/config")
async def post_config(req: ConfigReq) -> dict[str, Any]:
    cfg = gated_node.config
    mapping = {
        "pricing_mode":   "axl402_pricing_mode",
        "amount":         "x402_amount",
        "base_amount":    "x402_base_amount",
        "pricing_curve":  "axl402_pricing_curve",
        "pricing_n":      "axl402_pricing_n",
        "max_surge":      "axl402_max_surge",
        "quote_ttl_secs": "axl402_quote_ttl_secs",
        "quota_ttl_secs": "axl402_quota_ttl_secs",
    }
    changed = []
    for k, v in req.dict(exclude_unset=True).items():
        if v is None:
            continue
        target = mapping[k]
        if cfg.get(target) != v:
            cfg[target] = v
            changed.append(f"{k}={v}")
    if not changed:
        return {"changed": [], "message": "no changes"}
    bus.emit("system", "config update: " + ", ".join(changed))
    await gated_node.restart()
    return {"changed": changed}


class CallReq(BaseModel):
    mode: str  # "stock_mcp" | "x402_native" | "x402_pay"
    method: str = "tools/list"
    params: dict[str, Any] = {}
    service: str = "echo"


@app.post("/api/call")
async def call(req: CallReq) -> dict[str, Any]:
    peer = await _gated_pubkey()
    started = time.time()

    if req.mode == "stock_mcp":
        url = f"{CALLER_AXL}/mcp/{peer}/{req.service}"
        body = {"jsonrpc": "2.0", "id": int(started * 1000) % 100000,
                "method": req.method, "params": req.params}
        bus.emit("probe", f"stock MCP → {req.service}/{req.method}")
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, json=body)
        out = r.json()
        is_402 = (
            isinstance(out, dict)
            and isinstance(out.get("error"), dict)
            and out["error"].get("code") == -32402
        )
        if is_402:
            reqs = out["error"]["data"]["accepts"][0]
            bus.emit("402", f"stock MCP got 402 at price {reqs['maxAmountRequired']}", {"reqs": reqs})
        else:
            bus.emit("ok", "stock MCP got tool result (paid quota)")
        return {"shape": "stock_mcp", "response": out, "elapsed_s": time.time() - started}

    if req.mode in ("x402_native", "x402_pay"):
        url = f"{CALLER_AXL}/axl402/{peer}"
        envelope = {
            "v": 1,
            "mcp": {
                "service": req.service,
                "request": {"jsonrpc": "2.0", "id": 1,
                            "method": req.method, "params": req.params},
            },
        }
        bus.emit("probe", f"x402 envelope → {req.service}/{req.method}")
        async with httpx.AsyncClient(timeout=30.0) as c:
            probe_r = await c.post(url, json=envelope)
        probe = probe_r.json()

        if req.mode == "x402_native":
            bus.emit("402" if probe.get("accepts") else "ok",
                     f"x402 native: {'402' if probe.get('accepts') else 'ok'}")
            return {"shape": "x402_native", "response": probe, "elapsed_s": time.time() - started}

        if not probe.get("accepts"):
            bus.emit("ok", "x402 pay: peer didn't ask for payment (already in quota)")
            return {"shape": "x402_pay", "response": probe, "elapsed_s": time.time() - started}

        reqs = probe["accepts"][0]
        bus.emit("sign", f"signing EIP-3009 for {reqs['maxAmountRequired']} on {reqs['network']}")
        try:
            payload = sign_payment(reqs)
        except Exception as e:
            bus.emit("error", f"sign failed: {e}")
            return {"shape": "x402_pay", "error": f"sign: {e}"}
        envelope["payment"] = {"scheme": reqs["scheme"],
                               "network": reqs["network"], "payload": payload}
        bus.emit("settle", "submitting payment to facilitator (verify + on-chain settle)")
        async with httpx.AsyncClient(timeout=60.0) as c:
            pay_r = await c.post(url, json=envelope)
        out = pay_r.json()
        if out.get("ok"):
            tx = out.get("txHash", "")
            bus.emit("paid", f"on-chain settled: {tx}", {"tx": tx, "reqs": reqs})
        else:
            bus.emit("error", f"paid call failed: {out.get('error')}", {"out": out})
        return {"shape": "x402_pay", "probe": probe, "response": out,
                "elapsed_s": time.time() - started}

    return {"error": f"unknown mode: {req.mode}"}


class SignReq(BaseModel):
    requirements: dict[str, Any]


@app.post("/api/sign")
async def sign(req: SignReq) -> dict[str, Any]:
    bus.emit("sign", f"signing EIP-3009 for {req.requirements.get('maxAmountRequired')} on {req.requirements.get('network')}")
    try:
        payload = sign_payment(req.requirements)
    except Exception as e:
        bus.emit("error", f"sign failed: {e}")
        return {"error": f"sign: {e}"}
    return {
        "scheme":  req.requirements["scheme"],
        "network": req.requirements["network"],
        "payload": payload,
    }


class SubmitReq(BaseModel):
    service: str = "echo"
    method: str = "tools/list"
    params: dict[str, Any] = {}
    payment: Optional[dict[str, Any]] = None


@app.post("/api/submit")
async def submit(req: SubmitReq) -> dict[str, Any]:
    peer = await _gated_pubkey()
    url = f"{CALLER_AXL}/axl402/{peer}"
    envelope: dict[str, Any] = {
        "v": 1,
        "mcp": {
            "service": req.service,
            "request": {"jsonrpc": "2.0", "id": 1,
                        "method": req.method, "params": req.params},
        },
    }
    if req.payment:
        envelope["payment"] = req.payment
        bus.emit("settle", "submitting payment to facilitator (verify + on-chain settle)")
    else:
        bus.emit("probe", f"x402 envelope (no payment) → {req.service}/{req.method}")
    started = time.time()
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(url, json=envelope)
    out = r.json()
    if req.payment:
        if out.get("ok"):
            tx = out.get("txHash", "")
            bus.emit("paid", f"on-chain settled: {tx}", {"tx": tx})
        else:
            bus.emit("error", f"paid call failed: {out.get('error')}")
    else:
        bus.emit("402" if out.get("accepts") else "ok",
                 "x402 native: " + ("402" if out.get("accepts") else "ok"))
    return {"response": out, "elapsed_s": time.time() - started}


class AttackReq(BaseModel):
    n: int = 10
    service: str = "echo"


@app.post("/api/attack")
async def attack(req: AttackReq) -> dict[str, Any]:
    """Each attacker behaves like a determined client: keep re-probing,
    keep paying whatever the gate's latest 402 advertises, until the gate
    accepts the payment.

    With the new optimistic-dispatch gate, the response returns as soon as
    facilitator.Verify passes (~hundreds of ms). On-chain settlement happens
    asynchronously on the gate side. So attackers can run fully concurrently
    — no client-side serialization needed."""
    peer = await _gated_pubkey()
    n = max(1, min(50, req.n))
    MAX_RETRIES = 6
    bus.emit("attack", f"launching {n} concurrent attackers · gate dispatches on verify · settle is async")

    import random

    async def one(i: int) -> dict[str, Any]:
        url = f"{CALLER_AXL}/axl402/{peer}"
        envelope_base = {"v": 1, "mcp": {
            "service": req.service,
            "request": {"jsonrpc": "2.0", "id": i,
                        "method": "tools/list", "params": {}},
        }}
        last_err: Optional[str] = None
        async with httpx.AsyncClient(timeout=90.0) as c:
            for attempt in range(MAX_RETRIES):
                envelope = dict(envelope_base)

                # 1. Probe → get fresh PaymentRequirements at the current price.
                try:
                    probe = (await c.post(url, json=envelope)).json()
                except Exception as e:
                    last_err = f"probe: {e}"
                    await asyncio.sleep(0.5 + random.random()); continue

                if not probe.get("accepts"):
                    return {"i": i, "ok": True, "skip": "in_quota", "attempts": attempt + 1}

                reqs = probe["accepts"][0]
                price_atomic = reqs["maxAmountRequired"]

                # 2. Sign EXACTLY what the 402 quoted. No overpay (the
                # facilitator's "exact" scheme rejects mismatched amounts).
                try:
                    payload = sign_payment(reqs)
                except Exception as e:
                    last_err = f"sign: {e}"
                    await asyncio.sleep(0.5 + random.random()); continue
                envelope["payment"] = {
                    "scheme": reqs["scheme"],
                    "network": reqs["network"],
                    "payload": payload,
                }

                # 3. Submit. The gate runs verify (off-chain, ~ms) and
                #    immediately dispatches; on-chain settle is queued on
                #    the gate side. So this is just a fast HTTP roundtrip.
                try:
                    r = (await c.post(url, json=envelope)).json()
                except Exception as e:
                    last_err = f"submit: {e}"
                    await asyncio.sleep(0.5 + random.random()); continue

                # Success
                if r.get("ok"):
                    tx = r.get("txHash", "")
                    bus.emit("paid",
                             f"attack #{i}: paid {price_atomic} on attempt {attempt + 1} · tx {tx[:18]}…",
                             {"tx": tx, "price": price_atomic, "attempts": attempt + 1})
                    return {"i": i, "ok": True, "tx": tx,
                            "price": price_atomic, "attempts": attempt + 1}

                # Anything non-OK is treated as RETRYABLE: the user said
                # "just pay whatever the 402 returns", so we keep going.
                # Errors like "invalid_exact_evm_authorization_value" mean
                # surge moved past our quote between sign and verify; the
                # right response is to re-probe at the new price.
                # Errors like "replacement transaction underpriced" mean the
                # facilitator wallet was busy; back off and try again.
                err = r.get("error", "")
                new_price = (r.get("accepts") or [{}])[0].get("maxAmountRequired") if r.get("accepts") else None
                if new_price:
                    bus.emit("402",
                             f"attack #{i}: surge moved {price_atomic}→{new_price}, re-paying")
                    last_err = f"surge {price_atomic}→{new_price}"
                else:
                    short = (err.splitlines()[0] if err else "unknown")[:80]
                    bus.emit("402", f"attack #{i}: refused ({short}), retrying")
                    last_err = short
                # Jittered backoff so concurrent retries don't keep colliding.
                await asyncio.sleep(0.8 + random.random() * 1.2)

        bus.emit("error", f"attack #{i}: out of retries · last={last_err}")
        return {"i": i, "ok": False, "error": f"out of retries: {last_err}",
                "attempts": MAX_RETRIES}

    results = await asyncio.gather(*[one(i) for i in range(n)])
    paid    = [r for r in results if r.get("ok") and r.get("tx")]
    total_atomic = sum(int(r["price"]) for r in paid)
    bus.emit("attack_done",
             f"attack: {len(paid)}/{n} paid · "
             f"spent {total_atomic} atomic ({total_atomic / 1_000_000:.4f} USDC)")
    return {"results": results, "total_atomic": total_atomic}


@app.get("/api/events")
async def events() -> StreamingResponse:
    async def gen() -> AsyncIterator[bytes]:
        async for ev in bus.subscribe():
            data = json.dumps({"ts": ev.ts, "kind": ev.kind,
                               "msg": ev.msg, "extra": ev.extra})
            yield f"data: {data}\n\n".encode()
    return StreamingResponse(gen(), media_type="text/event-stream")


# ── Static frontend ────────────────────────────────────────────────────────


@app.get("/")
async def root() -> HTMLResponse:
    with open(INDEX_PATH, "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8080, reload=False)
