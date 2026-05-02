#!/usr/bin/env bash
# AXL402 demo orchestrator. Starts:
#   1. fake MCP router (./fake_mcp_router.py)         on :9103
#   2. caller AXL node (ungated)                       on :9002 (configs/node-A.json)
#   3. gated AXL node (surge mode)                     on :9012 (configs/node-B-surge.json)
#   4. FastAPI dashboard (./server.py)                 on :8080
#
# Sets up a Python venv (./.venv) on first run.
# Stops everything on Ctrl-C.
#
# Required:
#   PRIVATE_KEY           Base Sepolia wallet private key (used to sign EIP-3009)
#
# Optional:
#   AXL402_BINARY         path to the compiled AXL402 `node` binary
#                         (default: ../axl/node)
#   AXL402_MODE           "fixed" | "surge"  (default: surge)
#                         picks configs/node-B-{mode}.json for the gated node
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -z "${PRIVATE_KEY:-}" ]]; then
  echo "ERROR: PRIVATE_KEY env var must be set (Base Sepolia wallet, used to sign EIP-3009)." >&2
  exit 1
fi

NODE_BIN="${AXL402_BINARY:-$ROOT/../axl/node}"
if [[ ! -x "$NODE_BIN" ]]; then
  echo "ERROR: AXL402 binary not found at $NODE_BIN" >&2
  echo "Set AXL402_BINARY=/path/to/node, or build the AXL402 fork:" >&2
  echo "    git clone https://github.com/lordshashank/axl    # the AXL402 fork" >&2
  echo "    cd axl && make build                              # produces ./node" >&2
  exit 1
fi

MODE="${AXL402_MODE:-surge}"
GATED_CONFIG="$ROOT/configs/node-B-${MODE}.json"
CALLER_CONFIG="$ROOT/configs/node-A.json"
if [[ ! -f "$GATED_CONFIG" ]]; then
  echo "ERROR: gated config not found: $GATED_CONFIG" >&2
  exit 1
fi

VENV="$ROOT/.venv"
if [[ ! -d "$VENV" ]]; then
  echo "→ creating Python venv at $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip --quiet
  "$VENV/bin/pip" install -r "$ROOT/requirements.txt" --quiet
fi
PY="$VENV/bin/python"

PIDS=()
cleanup() {
  echo
  echo "stopping demo processes…"
  for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done
  pkill -f "node -config $CALLER_CONFIG" 2>/dev/null || true
  pkill -f "node -config $GATED_CONFIG"  2>/dev/null || true
  pkill -f "$ROOT/fake_mcp_router.py"    2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ fake MCP router on :9103"
"$PY" "$ROOT/fake_mcp_router.py" > /tmp/router.log 2>&1 &
PIDS+=($!)
until curl -fsS -X POST http://127.0.0.1:9103/route \
      -H "Content-Type: application/json" \
      -d '{"service":"echo","request":{"jsonrpc":"2.0","id":1,"method":"tools/list"}}' \
      >/dev/null 2>&1; do sleep 0.3; done

echo "→ caller AXL on :9002 (ungated)"
"$NODE_BIN" -config "$CALLER_CONFIG" > /tmp/axl-A.log 2>&1 &
PIDS+=($!)
until curl -fsS http://127.0.0.1:9002/topology >/dev/null 2>&1; do sleep 1; done

echo "→ gated AXL on :9012 ($MODE mode)"
"$NODE_BIN" -config "$GATED_CONFIG" > /tmp/axl-B.log 2>&1 &
PIDS+=($!)
until curl -fsS http://127.0.0.1:9012/topology >/dev/null 2>&1; do sleep 1; done

echo "→ dashboard at http://127.0.0.1:8080"
exec "$PY" -m uvicorn server:app --host 127.0.0.1 --port 8080
