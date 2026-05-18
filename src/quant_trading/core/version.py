"""
Global system version manifest (core-004).
AGENTS.md §28: Version manifest with all component versions.
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from typing import Any
import pydantic
import numpy, pandas

UTC = timezone.utc

def generate_manifest(config_hash: str = "") -> dict[str, Any]:
    return {
        "version": "0.1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "pydantic": pydantic.__version__,
        "numpy": numpy.__version__,
        "pandas": pandas.__version__,
        "config_hash": config_hash,
    }

def hash_manifest(manifest: dict) -> str:
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
