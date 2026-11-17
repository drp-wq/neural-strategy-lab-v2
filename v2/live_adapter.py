"""Fail-closed placeholder for the permanently disabled Phase 4 adapter.

The current lifecycle authorizes paper trading only. This module intentionally
contains no credential access, signing code, or network write capability.
"""

from __future__ import annotations

from typing import Any


class LiveExecutionAdapter:
    """A hard-disabled adapter; live execution requires a separately reviewed implementation."""

    def __init__(self, market: str = "SPOT", **_kwargs: Any) -> None:
        self.market = market.upper()
        self.kill_switch_active = False

    def is_ready(self) -> tuple[bool, str]:
        return False, "paper_only_hard_rule"

    def activate_kill_switch(self) -> None:
        self.kill_switch_active = True

    def place_market_order(self, symbol: str, side: str, quantity: float) -> dict[str, Any]:
        return {"executed": False, "reason": "paper_only_hard_rule"}
