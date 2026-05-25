"""Historical order/fill replay validation with explicit verifier paths."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class ReplayMode(str, Enum):
    DECISION = "decision"
    ORDER_MATCHING = "order_matching"
    FULL = "full"


@dataclass(frozen=True)
class ReplayDifference:
    index: int
    expected: dict[str, Any]
    actual: dict[str, Any]


@dataclass
class ReplayResult:
    mode: ReplayMode
    orders_replayed: int = 0
    matches: int = 0
    mismatches: int = 0
    differences: list[ReplayDifference] = field(default_factory=list)
    input_hash: str = ""

    @property
    def match_rate(self) -> float:
        return self.matches / max(self.orders_replayed, 1)

    def to_report(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "orders_replayed": self.orders_replayed,
            "matches": self.matches,
            "mismatches": self.mismatches,
            "match_rate": round(self.match_rate, 8),
            "input_hash": self.input_hash,
            "differences": [asdict(item) for item in self.differences],
        }


_VALIDATOR_METHOD = {
    ReplayMode.DECISION: "decide",
    ReplayMode.ORDER_MATCHING: "match_order",
    ReplayMode.FULL: "replay_full",
}


def replay_orders(
    historical_orders: Sequence[dict[str, Any]],
    replay_engine: object,
    mode: ReplayMode = ReplayMode.DECISION,
) -> ReplayResult:
    """Replay immutable historical inputs through an explicit validation API."""

    method_name = _VALIDATOR_METHOD[mode]
    validator = getattr(replay_engine, method_name, None)
    if not callable(validator):
        raise ValueError(f"replay engine must implement {method_name}() for {mode.value} mode")
    source = [dict(order) for order in historical_orders]
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    result = ReplayResult(mode=mode, orders_replayed=len(source), input_hash=digest)
    for index, expected in enumerate(source):
        actual = validator(dict(expected))
        if not isinstance(actual, dict):
            raise TypeError("replay engine result must be a dictionary")
        if actual == expected:
            result.matches += 1
        else:
            result.mismatches += 1
            result.differences.append(ReplayDifference(index, expected, actual))
    return result
