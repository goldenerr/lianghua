"""Recursive and redacted multi-environment config drift detection."""
from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

_MISSING = "<missing>"
_REDACTED = "<redacted>"
_SENSITIVE_TERMS = ("secret", "token", "password", "passphrase", "api_key", "credential")


def detect_drift(
    dev_config: str | Path,
    prod_config: str | Path,
    allowed_paths: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare YAML recursively and return a deploy-blocking redacted report.

    ``allowed_paths`` accepts shell-style path patterns, for example
    ``api.exchanges.*.base_url`` for deliberately environment-specific endpoints.
    """
    dev = _read_yaml(dev_config)
    prod = _read_yaml(prod_config)
    differences: dict[str, dict[str, Any]] = {}
    _walk("", dev, prod, differences)

    allowlist = tuple(allowed_paths)
    allowed = sorted(path for path in differences if _is_allowed(path, allowlist))
    blocked = sorted(path for path in differences if path not in allowed)
    return {
        "drifting_keys": sorted(differences),
        "differences": differences,
        "allowed_keys": allowed,
        "blocked_keys": blocked,
        "count": len(differences),
        "blocked": bool(blocked),
    }


def _read_yaml(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"configuration file not found: {source}")
    parsed = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(parsed, dict):
        raise ValueError(f"configuration root must be a mapping: {source}")
    return parsed


def _walk(path: str, dev: Any, prod: Any, differences: dict[str, dict[str, Any]]) -> None:
    if isinstance(dev, dict) and isinstance(prod, dict):
        for key in sorted(set(dev) | set(prod)):
            child = f"{path}.{key}" if path else str(key)
            _walk(child, dev.get(key, _MISSING), prod.get(key, _MISSING), differences)
        return
    if dev != prod:
        differences[path] = {"dev": _mask(path, dev), "prod": _mask(path, prod)}


def _mask(path: str, value: Any) -> Any:
    lower_path = path.lower()
    if any(term in lower_path for term in _SENSITIVE_TERMS):
        return _REDACTED
    return value


def _is_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in allowed_paths)
