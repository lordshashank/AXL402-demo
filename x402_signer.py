"""
Real EIP-3009 / x402 signer for the `exact` scheme on EVM (Base Sepolia).

Reads a private key from the PRIVATE_KEY env var. Never prints, logs, or
otherwise exposes the key — it is only used to construct an in-memory
LocalAccount and to sign the EIP-712 TransferWithAuthorization payload.

The signed payload format matches what the Coinbase facilitator expects
when calling /verify and /settle. See:
    https://github.com/coinbase/x402

Returned payload shape (matches facilitator API):
    {
      "x402Version": 1,
      "scheme":      "exact",
      "network":     "base-sepolia",
      "payload": {
        "signature":     "0x..." (65-byte hex),
        "authorization": {
          "from":         "0x...",
          "to":           "0x...",
          "value":        "<atomic units>",
          "validAfter":   "<unix>",
          "validBefore":  "<unix>",
          "nonce":        "0x... (32 bytes)"
        }
      }
    }
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import time
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data


NETWORK_TO_CHAIN_ID = {
    "base":         8453,
    "base-sepolia": 84532,
    # add more as needed
}


def address_from_env() -> str:
    pk = os.environ.get("PRIVATE_KEY")
    if not pk:
        raise RuntimeError("PRIVATE_KEY env var not set")
    return Account.from_key(pk).address


def sign_payment(reqs: dict[str, Any]) -> str:
    """Build and sign a TransferWithAuthorization message matching `reqs`.

    Returns a base64-encoded JSON string suitable as the `payload` field of
    an x402 envelope's `payment` block.
    """
    pk = os.environ.get("PRIVATE_KEY")
    if not pk:
        raise RuntimeError("PRIVATE_KEY env var not set")
    acct = Account.from_key(pk)

    network = reqs["network"]
    chain_id = NETWORK_TO_CHAIN_ID.get(network)
    if chain_id is None:
        raise RuntimeError(f"unknown network for chain id mapping: {network!r}")

    asset = reqs["asset"]                    # USDC contract
    pay_to = reqs["payTo"]
    value = str(reqs["maxAmountRequired"])
    extra = reqs.get("extra") or {}
    extra_name = extra.get("name", "USDC")
    extra_version = extra.get("version", "2")

    now = int(time.time())
    valid_after = 0
    valid_before = now + int(reqs.get("maxTimeoutSeconds", 60))

    # 32-byte random nonce, must be unique per authorization for this from/asset.
    nonce_bytes = secrets.token_bytes(32)
    nonce_hex = "0x" + nonce_bytes.hex()

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from",        "type": "address"},
                {"name": "to",          "type": "address"},
                {"name": "value",       "type": "uint256"},
                {"name": "validAfter",  "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce",       "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {
            "name":              extra_name,
            "version":           extra_version,
            "chainId":           chain_id,
            "verifyingContract": asset,
        },
        "message": {
            "from":        acct.address,
            "to":          pay_to,
            "value":       int(value),
            "validAfter":  valid_after,
            "validBefore": valid_before,
            "nonce":       nonce_bytes,
        },
    }

    signed = acct.sign_message(encode_typed_data(full_message=typed_data))
    sig_hex = signed.signature.hex()
    if not sig_hex.startswith("0x"):
        sig_hex = "0x" + sig_hex

    payload_obj = {
        "x402Version": 1,
        "scheme":  "exact",
        "network": network,
        "payload": {
            "signature": sig_hex,
            "authorization": {
                "from":        acct.address,
                "to":          pay_to,
                "value":       value,
                "validAfter":  str(valid_after),
                "validBefore": str(valid_before),
                "nonce":       nonce_hex,
            },
        },
    }

    return base64.b64encode(json.dumps(payload_obj).encode("utf-8")).decode("ascii")
