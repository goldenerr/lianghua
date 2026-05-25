"""Versioned factor metadata and parity verification baseline for factor-001."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class FactorDefinition:
    name: str
    entity: str = "symbol"
    dtype: str = "float64"
    refresh_interval: str = "daily"
    version: str = "v1"
    corporate_action_adjusted: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip():
            raise ValueError("factor name and version are required")
        if not self.corporate_action_adjusted:
            raise ValueError("unadjusted factors cannot be registered for trading")


@dataclass
class FactorStore:
    """Local parity-checking adapter; production Feast deployment remains gated."""

    factors: dict[str, FactorDefinition] = field(default_factory=dict)
    _versions: dict[str, dict[str, FactorDefinition]] = field(default_factory=dict)
    _offline_hashes: dict[tuple[str, str], str] = field(default_factory=dict)
    _online_hashes: dict[tuple[str, str], str] = field(default_factory=dict)

    @staticmethod
    def _hash_values(values: Mapping[str, Any]) -> str:
        serialized = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def register(self, factor: FactorDefinition) -> None:
        versions = self._versions.setdefault(factor.name, {})
        if factor.version in versions and versions[factor.version] != factor:
            raise ValueError("a published factor version is immutable")
        versions[factor.version] = factor
        self.factors[factor.name] = factor

    def get(self, name: str, version: str | None = None) -> FactorDefinition | None:
        if version is None:
            return self.factors.get(name)
        return self._versions.get(name, {}).get(version)

    def publish_offline_values(self, name: str, version: str, values: Mapping[str, Any]) -> str:
        if self.get(name, version) is None:
            raise KeyError("factor version is not registered")
        digest = self._hash_values(values)
        self._offline_hashes[(name, version)] = digest
        return digest

    def publish_online_values(self, name: str, version: str, values: Mapping[str, Any]) -> str:
        if self.get(name, version) is None:
            raise KeyError("factor version is not registered")
        digest = self._hash_values(values)
        self._online_hashes[(name, version)] = digest
        return digest

    def verify_offline_online_parity(self, name: str, version: str) -> bool:
        key = (name, version)
        if key not in self._offline_hashes or key not in self._online_hashes:
            raise ValueError("both offline and online factor snapshots are required")
        return self._offline_hashes[key] == self._online_hashes[key]
