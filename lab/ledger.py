"""Independent SPOT (25 USDT) and FUTURES (25 USDT) paper-only ledgers."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

LedgerType = Literal["SPOT", "FUTURES"]
SideType = Literal["LONG", "SHORT"]


@dataclass
class SymbolRules:
    """Exchange LOT_SIZE and MIN_NOTIONAL rules."""

    min_qty: float = 0.0001
    step_size: float = 0.0001
    min_notional: float = 5.0


@dataclass
class PaperPosition:
    """Simulated spot or isolated futures position."""

    symbol: str
    ledger: LedgerType
    side: SideType
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    leverage: float = 1.0
    margin_used: float = 0.0
    liquidation_price: float = 0.0
    opened_at_utc: int = 0
    strategy: str = ""
    category: str = ""
    unrealized_pnl: float = 0.0

    def update_mark(self, mark_price: float) -> float:
        if self.side == "LONG":
            self.unrealized_pnl = (mark_price - self.entry_price) * self.qty
        else:
            self.unrealized_pnl = (self.entry_price - mark_price) * self.qty
        return self.unrealized_pnl

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "ledger": self.ledger,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "leverage": self.leverage,
            "margin_used": self.margin_used,
            "liquidation_price": self.liquidation_price,
            "opened_at_utc": self.opened_at_utc,
            "strategy": self.strategy,
            "category": self.category,
            "unrealized_pnl": self.unrealized_pnl,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperPosition:
        return cls(**d)


class PaperLedger:
    """Independent paper ledger enforcing risk, exposure, leverage, and exchange rules."""

    def __init__(
        self,
        ledger_type: LedgerType,
        starting_capital: float = 25.0,
        max_risk_per_trade_pct: float = 0.01,
        max_exposure_pct: float = 0.40,
        daily_dd_halt_pct: float = 0.05,
        max_positions: int = 3,
        max_leverage: float = 2.0,
        fee_rate: float | None = None,
    ) -> None:
        self.ledger_type: LedgerType = ledger_type
        self.starting_capital = starting_capital
        self.cash_usdt = starting_capital
        self.daily_start_equity = starting_capital
        self.trading_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.daily_halted = False

        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_exposure_pct = max_exposure_pct
        self.daily_dd_halt_pct = daily_dd_halt_pct
        self.max_positions = max_positions
        self.max_leverage = min(max_leverage, 2.0) if ledger_type == "FUTURES" else 1.0
        self.fee_rate = fee_rate if fee_rate is not None else (0.001 if ledger_type == "SPOT" else 0.0004)

        self.positions: dict[str, PaperPosition] = {}
        self.realized_pnl: float = 0.0
        self.total_fees: float = 0.0
        self.total_funding: float = 0.0
        self.closed_trades: list[dict[str, Any]] = []

    def equity(self) -> float:
        unrealized = sum(p.unrealized_pnl for p in self.positions.values())
        locked_margin = sum(p.margin_used for p in self.positions.values())
        return self.cash_usdt + locked_margin + unrealized

    def current_exposure(self) -> float:
        return sum(p.qty * p.entry_price for p in self.positions.values())

    def check_daily_halt(self) -> tuple[bool, str]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        eq = self.equity()
        if self.trading_day != today:
            self.trading_day = today
            self.daily_start_equity = eq
            self.daily_halted = False
        if self.daily_halted:
            return False, "daily_drawdown_halt_active"
        if self.daily_start_equity > 0:
            dd = (self.daily_start_equity - eq) / self.daily_start_equity
            if dd >= self.daily_dd_halt_pct:
                self.daily_halted = True
                return False, "daily_drawdown_5pct_halt"
        return True, "ok"

    def open_position(
        self,
        symbol: str,
        side: SideType,
        bid: float,
        ask: float,
        stop_loss: float,
        take_profit: float,
        rules: SymbolRules,
        ts_utc: int,
        leverage: float = 1.0,
        slippage_bps: float = 5.0,
        strategy: str = "",
        category: str = "",
    ) -> tuple[bool, str, PaperPosition | None]:
        """Attempt to open a paper position; SKIP if min_notional or risk rules fail."""
        if self.ledger_type == "SPOT" and side == "SHORT":
            return False, "spot_long_only", None

        ok_halt, halt_reason = self.check_daily_halt()
        if not ok_halt:
            return False, halt_reason, None

        if symbol in self.positions:
            return False, "position_already_open", None
        if len(self.positions) >= self.max_positions:
            return False, "max_3_positions_reached", None

        lev = 1.0 if self.ledger_type == "SPOT" else min(max(1.0, leverage), self.max_leverage)
        slip = slippage_bps / 10_000.0
        fill_price = ask * (1.0 + slip) if side == "LONG" else bid * (1.0 - slip)
        if fill_price <= 0:
            return False, "invalid_fill_price", None

        stop_dist = abs(fill_price - stop_loss)
        if stop_dist <= 0:
            return False, "invalid_stop_loss", None

        eq = self.equity()
        risk_budget = eq * self.max_risk_per_trade_pct
        raw_qty = risk_budget / stop_dist

        # Cap by max concurrent exposure (40% of equity * leverage)
        max_new_notional = max(0.0, eq * self.max_exposure_pct * lev - self.current_exposure())
        if max_new_notional <= 0:
            return False, "max_exposure_40pct_reached", None

        raw_qty = min(raw_qty, max_new_notional / fill_price)

        # Round to exchange step_size
        if rules.step_size > 0:
            qty = round(math.floor(raw_qty / rules.step_size) * rules.step_size, 8)
        else:
            qty = raw_qty

        if qty < rules.min_qty:
            return False, "skip_below_min_qty", None
        notional = qty * fill_price
        if notional < rules.min_notional:
            return False, "skip_below_min_notional", None

        margin_req = notional / lev
        fee = notional * self.fee_rate
        if margin_req + fee > self.cash_usdt:
            return False, "insufficient_ledger_cash", None

        self.cash_usdt -= margin_req + fee
        self.total_fees += fee

        liq_price = 0.0
        if self.ledger_type == "FUTURES":
            liq_price = (
                fill_price * (1.0 - 0.9 / lev)
                if side == "LONG"
                else fill_price * (1.0 + 0.9 / lev)
            )

        pos = PaperPosition(
            symbol=symbol,
            ledger=self.ledger_type,
            side=side,
            qty=qty,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=lev,
            margin_used=margin_req,
            liquidation_price=liq_price,
            opened_at_utc=ts_utc,
            strategy=strategy,
            category=category,
        )
        self.positions[symbol] = pos
        return True, "filled", pos

    def close_position(
        self,
        symbol: str,
        bid: float,
        ask: float,
        reason: str,
        ts_utc: int,
        slippage_bps: float = 5.0,
    ) -> dict[str, Any] | None:
        pos = self.positions.pop(symbol, None)
        if pos is None:
            return None

        slip = slippage_bps / 10_000.0
        exit_price = bid * (1.0 - slip) if pos.side == "LONG" else ask * (1.0 + slip)
        if pos.side == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.qty

        fee = (pos.qty * exit_price) * self.fee_rate
        net_pnl = gross_pnl - fee
        self.cash_usdt += pos.margin_used + net_pnl
        self.realized_pnl += net_pnl
        self.total_fees += fee

        record = {
            "symbol": symbol,
            "ledger": self.ledger_type,
            "side": pos.side,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "net_pnl": net_pnl,
            "fee": fee,
            "reason": reason,
            "opened_at_utc": pos.opened_at_utc,
            "closed_at_utc": ts_utc,
            "strategy": pos.strategy,
            "category": pos.category,
        }
        self.closed_trades.append(record)
        return record
