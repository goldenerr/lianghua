"""Multi-region failover (ops-002). AGENTS.md: cross-region sync, geo-redundancy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Region(str, Enum):
    PRIMARY = "us-east"
    SECONDARY = "ap-southeast"
    TERTIARY = "eu-west"


@dataclass
class FailoverManager:
    active: Region = Region.PRIMARY

    def __post_init__(self) -> None:
        self.last_failover_at: str | None = None
        self.region_health: dict[Region, bool] = {
            Region.PRIMARY: True,
            Region.SECONDARY: True,
            Region.TERTIARY: True,
        }
        self.replication_lag_seconds: float = 0.5

    def failover(self, to: Region) -> bool:
        if not self.region_health.get(to, False):
            return False
        self.active = to
        self.last_failover_at = datetime.now(timezone.utc).isoformat()
        return True

    def set_region_health(self, region: Region, healthy: bool) -> None:
        self.region_health[region] = healthy

    def set_replication_lag(self, lag_seconds: float) -> None:
        self.replication_lag_seconds = max(0.0, lag_seconds)

    def sync_status(self) -> dict:
        return {
            "active": self.active.value,
            "lag_seconds": self.replication_lag_seconds,
            "last_failover_at": self.last_failover_at,
            "all_regions_healthy": all(self.region_health.values()),
        }
