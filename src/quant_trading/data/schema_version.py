
"""Data schema version management (data-004). AGENTS.md: version compatibility enforcement."""
from dataclasses import dataclass
@dataclass
class SchemaVersion:
    version: str; compatible: list = None
    def is_compatible(self, target: str) -> bool: return self.compatible is None or target in self.compatible
