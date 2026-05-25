"""Signed global system version manifests for artifact verification (core-004)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy
import pandas
import pydantic

from quant_trading import __version__

UTC = timezone.utc
SIGNATURE_ALGORITHM = "hmac-sha256"


def generate_manifest(
    config_hash: str = "",
    strategy_code_hash: str = "",
    data_schema_version: str = "",
    component_versions: dict[str, str] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build the unsigned material tied to a deployable runtime artifact."""
    components = {
        "python": sys.version.split()[0],
        "pydantic": pydantic.__version__,
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
    }
    components.update(component_versions or {})
    return {
        "system_version": __version__,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "components": components,
        "config_hash": config_hash,
        "strategy_code_hash": strategy_code_hash,
        "data_schema_version": data_schema_version,
    }


def hash_tree(root: str | Path, patterns: tuple[str, ...] = ("*",)) -> str:
    """Hash an approved directory tree with stable relative paths and bytes."""
    directory = Path(root)
    if not directory.exists() or not directory.is_dir():
        raise ValueError(f"manifest input directory does not exist: {directory}")
    files: set[Path] = set()
    for pattern in patterns:
        files.update(path for path in directory.rglob(pattern) if path.is_file())
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda value: value.relative_to(directory).as_posix()):
        relative_path = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(relative_path)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def hash_manifest(manifest: dict[str, Any]) -> str:
    """Return a full digest of all manifest content except its signature."""
    return hashlib.sha256(_canonical_payload(manifest)).hexdigest()


def sign_manifest(manifest: dict[str, Any], signing_key: bytes, key_id: str) -> dict[str, Any]:
    """Create a signed copy without persisting the signing secret."""
    if not signing_key:
        raise ValueError("signing_key must not be empty")
    if not key_id.strip():
        raise ValueError("key_id must not be empty")
    signed = _without_signature(manifest)
    signed["manifest_hash"] = hash_manifest(signed)
    signed["signature"] = {
        "algorithm": SIGNATURE_ALGORITHM,
        "key_id": key_id,
        "value": hmac.new(signing_key, _canonical_payload(signed), hashlib.sha256).hexdigest(),
    }
    return signed


def verify_manifest(manifest: dict[str, Any], signing_key: bytes) -> bool:
    """Verify hash integrity and the signing key for a loaded manifest."""
    if not signing_key:
        return False
    signature = manifest.get("signature", {})
    if signature.get("algorithm") != SIGNATURE_ALGORITHM:
        return False
    unsigned = _without_signature(manifest)
    stored_hash = unsigned.pop("manifest_hash", "")
    if not hmac.compare_digest(stored_hash, hash_manifest(unsigned)):
        return False
    payload = dict(unsigned)
    payload["manifest_hash"] = stored_hash
    expected = hmac.new(signing_key, _canonical_payload(payload), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, str(signature.get("value", "")))


def write_signed_manifest(
    path: str | Path,
    manifest: dict[str, Any],
    signing_key: bytes,
    key_id: str,
) -> dict[str, Any]:
    """Atomically write a signed artifact manifest and return its contents."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    signed = sign_manifest(manifest, signing_key, key_id)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(signed, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(destination)
    return signed


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Read an artifact manifest for querying or verification."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _without_signature(manifest: dict[str, Any]) -> dict[str, Any]:
    payload = dict(manifest)
    payload.pop("signature", None)
    return payload


def _canonical_payload(manifest: dict[str, Any]) -> bytes:
    return json.dumps(
        _without_signature(manifest),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
