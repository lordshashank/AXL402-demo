#!/usr/bin/env bash
# AXL402 demo orchestrator. Starts:
#   1. dummy MCP router (./dummy_mcp_router.py) on :9103
#   2. caller AXL node (ungated, configs/node-A.json) on :9002
#   3. FastAPI dashboard on :8080
#
# The dashboard owns the GATED AXL node — it spawns it on startup and can
# restart it with new pricing params via the UI. So we no longer launch
# node-B-{fixed,surge}.json from this script.
#
# Required: PRIVATE_KEY (Base Sepolia wallet, used to sign EIP-3009)
# Optional: AXL402_BINARY (default: ../axl/node)
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ -z "${PRIVATE_KEY:-}" ]]; then
  echo "ERROR: PRIVATE_KEY env var must be set." >&2
  exit 1
fi

NODE_BIN="${AXL402_BINARY:-$ROOT/../axl/node}"
if [[ ! -x "$NODE_BIN" ]]; then
  echo "ERROR: AXL402 binary not found at $NODE_BIN" >&2
  echo "Build the AXL402 fork first: (cd ../axl && make build)" >&2
  exit 1
fi
export AXL402_BINARY="$NODE_BIN"

CALLER_CONFIG="$ROOT/configs/node-A.json"
if [[ ! -f "$CALLER_CONFIG" ]]; then
  echo "ERROR: caller config not found: $CALLER_CONFIG" >&2
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
  pkill -f "$ROOT/dummy_mcp_router.py"    2>/dev/null || true
  # The dashboard's startup hook started the gated node; its shutdown hook
  # will stop it cleanly when uvicorn exits. As a fallback:
  pkill -f "configs/_active.json" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ dummy MCP router on :9103"
"$PY" "$ROOT/dummy_mcp_router.py" > /tmp/router.log 2>&1 &
PIDS+=($!)
until curl -fsS -X POST http://127.0.0.1:9103/route \
      -H "Content-Type: application/json" \
      -d '{"service":"echo","request":{"jsonrpc":"2.0","id":1,"method":"tools/list"}}' \
      >/dev/null 2>&1; do sleep 0.3; done

echo "→ caller AXL on :9002 (ungated)"
"$NODE_BIN" -config "$CALLER_CONFIG" > /tmp/axl-A.log 2>&1 &
PIDS+=($!)
until curl -fsS http://127.0.0.1:9002/topology >/dev/null 2>&1; do sleep 1; done

echo "→ dashboard at http://127.0.0.1:8080 (it will start the gated node)"
exec "$PY" -m uvicorn server:app --host 127.0.0.1 --port 8080
