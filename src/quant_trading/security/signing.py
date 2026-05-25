"""HMAC integrity signatures for strategy and model artifacts (security-001)."""
from __future__ import annotations

import hashlib
import hmac


def sign_code(source: str | bytes, signing_key: bytes, key_id: str) -> dict[str, str]:
    """Sign source bytes using a secret obtained from an approved secret manager."""
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    if not key_id.strip():
        raise ValueError("key_id must not be empty")
    data = source.encode("utf-8") if isinstance(source, str) else source
    signature = hmac.new(signing_key, data, hashlib.sha256).hexdigest()
    return {"signature": signature, "algorithm": "hmac-sha256", "key_id": key_id}


def verify_code(source: str | bytes, signature: dict[str, str], signing_key: bytes) -> bool:
    """Reject modified code, unsupported algorithms, or incorrect signing keys."""
    if not signing_key or signature.get("algorithm") != "hmac-sha256":
        return False
    data = source.encode("utf-8") if isinstance(source, str) else source
    expected = hmac.new(signing_key, data, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.get("signature", ""))
