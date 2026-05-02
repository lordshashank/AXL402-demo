"""
FastAPI backend for the AXL402 demo.

Holds the signer (uses PRIVATE_KEY env var, never echoes it). Proxies the
local caller AXL node and the gated AXL node so the browser only talks
to this server. Adds an SSE event stream so the UI can show live activity.

Run via ./run.sh — that script also starts the fake MCP router and both
AXL nodes in the right order. Requires the AXL402 fork's compiled `node`
binary; set AXL402_BINARY (or place the binary at ../axl/node).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# Local signer (sibling file).
from x402_signer import sign_payment, address_from_env  # noqa: E402


CALLER_AXL = os.environ.get("CALLER_AXL", "http://127.0.0.1:9002")
GATED_AXL  = os.environ.get("GATED_AXL",  "http://127.0.0.1:9012")


@dataclass
class Event:
    ts: float
    kind: str
    msg: str
    extra: dict | None = None


class EventBus:
    def __init__(self) -> None:
        self.subscribers: list[asyncio.Queue[Event]] = []
        self.history: list[Event] = []

    def emit(self, kind: str, msg: str, extra: dict | None = None) -> None:
        ev = Event(ts=time.time(), kind=kind, msg=msg, extra=extra or {})
        self.history.append(ev)
        if len(self.history) > 200:
            self.history = self.history[-200:]
        for q in self.subscribers:
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    async def subscribe(self) -> AsyncIterator[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=128)
        self.subscribers.append(q)
        # Replay last 30 events so a fresh dashboard has context.
        for ev in self.history[-30:]:
            await q.put(ev)
        try:
            while True:
                yield await q.get()
        finally:
            self.subscribers.remove(q)


bus = EventBus()
app = FastAPI()


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
    try:
        # Confirm wallet is configured WITHOUT exposing the address
        # (treated as key-derived material per user policy).
        _ = address_from_env()
        out["wallet"] = {"ready": True}
    except Exception as e:
        out["wallet"] = {"ready": False, "error": str(e)}
    return out


@app.get("/api/price")
async def price() -> Any:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{GATED_AXL}/x402/price")
            return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


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
        url = f"{CALLER_AXL}/x402/{peer}"
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

        # Native probe (no pay) just returns whatever came back.
        if req.mode == "x402_native":
            bus.emit("402" if probe.get("accepts") else "ok",
                     f"x402 native: {'402' if probe.get('accepts') else 'ok'}")
            return {"shape": "x402_native", "response": probe, "elapsed_s": time.time() - started}

        # Pay flow:
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


class AttackReq(BaseModel):
    n: int = 10
    service: str = "echo"


@app.post("/api/attack")
async def attack(req: AttackReq) -> dict[str, Any]:
    peer = await _gated_pubkey()
    n = max(1, min(50, req.n))
    bus.emit("attack", f"launching {n} concurrent paid calls")

    async def one(i: int) -> dict[str, Any]:
        url = f"{CALLER_AXL}/x402/{peer}"
        envelope = {"v": 1, "mcp": {
            "service": req.service,
            "request": {"jsonrpc": "2.0", "id": i,
                        "method": "tools/list", "params": {}},
        }}
        async with httpx.AsyncClient(timeout=60.0) as c:
            probe = (await c.post(url, json=envelope)).json()
            if not probe.get("accepts"):
                return {"i": i, "ok": True, "skip": "in_quota"}
            reqs = probe["accepts"][0]
            try:
                payload = sign_payment(reqs)
            except Exception as e:
                return {"i": i, "ok": False, "error": f"sign: {e}"}
            envelope["payment"] = {
                "scheme": reqs["scheme"],
                "network": reqs["network"],
                "payload": payload,
            }
            try:
                r = (await c.post(url, json=envelope)).json()
                ok = bool(r.get("ok"))
                tx = r.get("txHash", "")
                bus.emit("paid" if ok else "error",
                         f"attack #{i}: {'tx ' + tx if ok else r.get('error', 'failed')}",
                         {"tx": tx, "price": reqs["maxAmountRequired"]})
                return {"i": i, "ok": ok, "tx": tx,
                        "price": reqs["maxAmountRequired"]}
            except Exception as e:
                return {"i": i, "ok": False, "error": str(e)}

    results = await asyncio.gather(*[one(i) for i in range(n)])
    total_atomic = sum(int(r.get("price", "0")) for r in results if r.get("ok"))
    bus.emit("attack_done",
             f"attack: {sum(1 for r in results if r.get('ok'))}/{n} paid, "
             f"total spent {total_atomic} atomic units")
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


HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(HERE, "index.html")


@app.get("/")
async def root() -> HTMLResponse:
    with open(INDEX_PATH, "r") as f:
        return HTMLResponse(f.read())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8080, reload=False)
