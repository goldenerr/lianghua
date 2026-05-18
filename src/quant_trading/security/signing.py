
"""Strategy/model code signing & integrity (security-003). AGENTS.md: code signature verification."""
import hashlib, json
def sign_code(source: str, key_id: str = "default") -> dict:
    h = hashlib.sha256((source+key_id).encode()).hexdigest()
    return {"hash": h, "algorithm": "sha256", "key_id": key_id}
def verify_code(source: str, signature: dict) -> bool:
    expected = hashlib.sha256((source+signature.get("key_id","")).encode()).hexdigest()
    return expected == signature.get("hash","")
