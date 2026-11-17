"""Independent SPOT and FUTURES paper-only ledgers with shared global exposure cap."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any, Literal

LedgerType = Literal["SPOT", "FUTURES"]
SideType = Literal["LONG", "SHORT"]


@dataclass(frozen=True)
class SymbolRules:
    """Exchange LOT_SIZE / MARKET_LOT_SIZE and NOTIONAL / MIN_NOTIONAL filters."""

    min_qty: float
    step_size: float
    min_notional: float
    max_qty: float = 1e12
    max_notional: float = 1e12
    source: str = "exchangeInfo"


def _step_round(value: float, step: float, mode: str) -> float:
    """Round value to exact multiple of step using Decimal to prevent float drift."""
    if step <= 0:
        return value
    d_val = Decimal(str(value))
    d_step = Decimal(str(step))
    rounding = ROUND_FLOOR if mode == "floor" else ROUND_CEILING
    units = (d_val / d_step).to_integral_value(rounding=rounding)
    return float(units * d_step)


def compute_target_qty(
    target_notional: float,
    fill_price: float,
    rules: SymbolRules,
    min_tolerance_notional: float,
    max_tolerance_notional: float,
    max_allowed_notional: float | None = None,
) -> tuple[bool, str, float, float]:
    """Compute step-aligned quantity targeting target_notional within tolerances.

    Never forces a fill outside exchange filters or notional tolerances.
    """
    if not all(math.isfinite(float(x)) for x in (target_notional, fill_price, min_tolerance_notional, max_tolerance_notional)):
        return False, "invalid_order_values", 0.0, 0.0
    if fill_price <= 0 or target_notional <= 0 or min_tolerance_notional < 0 or max_tolerance_notional < min_tolerance_notional:
        return False, "invalid_fill_price", 0.0, 0.0
    if not all(math.isfinite(float(x)) for x in (rules.min_qty, rules.step_size, rules.min_notional, rules.max_qty, rules.max_notional)) or rules.step_size <= 0 or rules.min_qty < 0 or rules.min_notional < 0 or rules.max_qty <= 0 or rules.max_notional <= 0:
        return False, "invalid_exchange_rules", 0.0, 0.0

    raw_qty = target_notional / fill_price
    if rules.step_size > 0:
        q_floor = _step_round(raw_qty, rules.step_size, "floor")
        q_ceil = _step_round(raw_qty, rules.step_size, "ceil")
        candidates = []
        for q in (q_floor, q_ceil):
            if q > 0 and q not in candidates:
                candidates.append(q)
    else:
        candidates = [raw_qty]

    # Evaluate candidates closest to target_notional that satisfy ALL rules & caps
    valid: list[tuple[float, float, float]] = []
    rejection_reasons: list[str] = []

    for q in candidates:
        notional = q * fill_price
        if q < rules.min_qty:
            rejection_reasons.append("skip_below_min_qty")
            continue
        if q > rules.max_qty:
            rejection_reasons.append("skip_above_max_qty")
            continue
        if notional < rules.min_notional:
            rejection_reasons.append("skip_below_min_notional")
            continue
        if notional > rules.max_notional:
            rejection_reasons.append("skip_above_max_notional")
            continue
        if max_allowed_notional is not None and notional > max_allowed_notional + 1e-9:
            rejection_reasons.append("global_exposure_cap_reached")
            continue
        if notional < min_tolerance_notional or notional > max_tolerance_notional:
            rejection_reasons.append("skip_outside_notional_tolerance")
            continue
        valid.append((abs(notional - target_notional), q, notional))

    if not valid:
        # Return most informative specific reason
        for preferred in (
            "global_exposure_cap_reached",
            "skip_below_min_notional",
            "skip_below_min_qty",
            "skip_outside_notional_tolerance",
        ):
            if preferred in rejection_reasons:
                return False, preferred, 0.0, 0.0
        return False, rejection_reasons[0] if rejection_reasons else "skip_invalid_qty", 0.0, 0.0

    valid.sort(key=lambda item: item[0])
    _, best_qty, best_notional = valid[0]
    return True, "ok", best_qty, best_notional


@dataclass
class PaperPosition:
    """Simulated Spot or Isolated USDT-M Futures position."""

    symbol: str
    ledger: LedgerType
    side: SideType
    qty: float
    entry_price: float
    target_notional: float
    actual_notional: float
    stop_loss: float
    take_profit: float
    leverage: float = 1.0
    margin_used: float = 0.0
    liquidation_price: float = 0.0
    entry_fee: float = 0.0
    accumulated_funding: float = 0.0
    last_funding_bucket: int = 0
    opened_at_utc: int = 0
    strategy: str = ""
    category: str = ""
    selection_bucket: str = ""
    route_reason: str = ""
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0

    def update_mark(self, mark_price: float) -> float:
        if mark_price <= 0:
            return self.unrealized_pnl
        self.mark_price = mark_price
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
            "target_notional": self.target_notional,
            "actual_notional": self.actual_notional,
            "stop_loss": self.stop_loss,
            "take_profit": self.take_profit,
            "leverage": self.leverage,
            "margin_used": self.margin_used,
            "liquidation_price": self.liquidation_price,
            "entry_fee": self.entry_fee,
            "accumulated_funding": self.accumulated_funding,
            "last_funding_bucket": self.last_funding_bucket,
            "opened_at_utc": self.opened_at_utc,
            "strategy": self.strategy,
            "category": self.category,
            "selection_bucket": self.selection_bucket,
            "route_reason": self.route_reason,
            "mark_price": self.mark_price,
            "unrealized_pnl": self.unrealized_pnl,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PaperPosition:
        return cls(**d)


class PaperLedger:
    """Independent paper ledger enforcing risk, margin, fees, funding, and daily DD halt."""

    def __init__(
        self,
        ledger_type: LedgerType,
        starting_capital: float = 2500.0,
        daily_dd_halt_pct: float = 0.05,
        max_leverage: float = 2.0,
        maintenance_margin_rate: float = 0.005,
        max_risk_per_trade_pct: float = 0.005,
        fee_rate: float | None = None,
        trading_day: str | None = None,
    ) -> None:
        self.ledger_type: LedgerType = ledger_type
        self.starting_capital = float(starting_capital)
        self.cash_usdt = float(starting_capital)
        self.daily_start_equity = float(starting_capital)
        self.trading_day = trading_day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.daily_halted = False

        self.daily_dd_halt_pct = float(daily_dd_halt_pct)
        self.max_leverage = min(float(max_leverage), 2.0) if ledger_type == "FUTURES" else 1.0
        self.maintenance_margin_rate = float(maintenance_margin_rate)
        self.max_risk_per_trade_pct = float(max_risk_per_trade_pct)
        self.fee_rate = (
            float(fee_rate)
            if fee_rate is not None
            else (0.001 if ledger_type == "SPOT" else 0.0004)
        )

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
        return sum(p.actual_notional for p in self.positions.values())

    def locked_margin(self) -> float:
        return sum(p.margin_used for p in self.positions.values())

    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    def check_daily_halt(self, now_utc: int | None = None) -> tuple[bool, str]:
        dt = (
            datetime.fromtimestamp(now_utc, tz=timezone.utc)
            if now_utc is not None
            else datetime.now(timezone.utc)
        )
        today = dt.strftime("%Y-%m-%d")
        eq = self.equity()
        if self.trading_day != today:
            self.trading_day = today
            self.daily_start_equity = eq
            self.daily_halted = False
        if self.daily_halted:
            return False, f"{self.ledger_type.lower()}_daily_drawdown_halt_active"
        if self.daily_start_equity > 0:
            dd = (self.daily_start_equity - eq) / self.daily_start_equity
            if dd >= self.daily_dd_halt_pct:
                self.daily_halted = True
                return False, f"{self.ledger_type.lower()}_daily_drawdown_halt_triggered"
        return True, "ok"

    def can_open(self, now_utc: int | None = None, min_cash_needed: float = 25.0) -> bool:
        ok_halt, _ = self.check_daily_halt(now_utc=now_utc)
        if not ok_halt:
            return False
        return self.cash_usdt >= min_cash_needed

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
        target_notional: float = 50.0,
        min_tolerance_notional: float = 45.0,
        max_tolerance_notional: float = 55.0,
        max_allowed_notional: float | None = None,
        leverage: float = 1.0,
        slippage_bps: float = 5.0,
        strategy: str = "",
        category: str = "",
        selection_bucket: str = "",
        route_reason: str = "",
        mark_price: float | None = None,
    ) -> tuple[bool, str, PaperPosition | None]:
        """Attempt to open a $50 target notional paper position respecting exchange filters."""
        if self.ledger_type == "SPOT" and side == "SHORT":
            return False, "spot_long_only", None

        ok_halt, halt_reason = self.check_daily_halt(now_utc=ts_utc)
        if not ok_halt:
            return False, halt_reason, None

        if symbol in self.positions:
            return False, "position_already_open", None

        if bid <= 0 or ask <= 0 or ask < bid:
            return False, "invalid_quote", None

        lev = 1.0 if self.ledger_type == "SPOT" else min(max(1.0, float(leverage)), self.max_leverage)
        slip = slippage_bps / 10_000.0
        fill_price = ask * (1.0 + slip) if side == "LONG" else bid * (1.0 - slip)
        if fill_price <= 0:
            return False, "invalid_fill_price", None

        if side == "LONG" and not (stop_loss < fill_price < take_profit):
            return False, "invalid_sl_tp_long", None
        if side == "SHORT" and not (take_profit < fill_price < stop_loss):
            return False, "invalid_sl_tp_short", None

        ok_qty, qty_reason, qty, notional = compute_target_qty(
            target_notional=target_notional,
            fill_price=fill_price,
            rules=rules,
            min_tolerance_notional=min_tolerance_notional,
            max_tolerance_notional=max_tolerance_notional,
            max_allowed_notional=max_allowed_notional,
        )
        if not ok_qty:
            return False, qty_reason, None

        # Risk budget check relative to allocated virtual capital
        stop_dist_pct = abs(fill_price - stop_loss) / fill_price
        dollar_risk = notional * stop_dist_pct
        max_dollar_risk = max(self.starting_capital, self.equity()) * self.max_risk_per_trade_pct
        if dollar_risk > max_dollar_risk:
            return False, "risk_budget_exceeded", None

        margin_req = notional / lev
        entry_fee = notional * self.fee_rate
        if margin_req + entry_fee > self.cash_usdt:
            return False, "insufficient_ledger_cash", None

        liq_price = 0.0
        if self.ledger_type == "FUTURES" and lev > 1.0:
            mmr = self.maintenance_margin_rate
            if side == "LONG":
                liq_price = fill_price * max(0.0, 1.0 - (1.0 / lev) + mmr)
                if stop_loss <= liq_price:
                    return False, "stop_loss_beyond_liquidation", None
            else:
                liq_price = fill_price * (1.0 + (1.0 / lev) - mmr)
                if stop_loss >= liq_price:
                    return False, "stop_loss_beyond_liquidation", None

        self.cash_usdt -= margin_req + entry_fee
        self.total_fees += entry_fee
        # Subtract entry fee from realized_pnl immediately so equity == starting_capital + realized + unrealized
        self.realized_pnl -= entry_fee

        init_mark = mark_price if (mark_price is not None and mark_price > 0) else fill_price
        pos = PaperPosition(
            symbol=symbol,
            ledger=self.ledger_type,
            side=side,
            qty=qty,
            entry_price=fill_price,
            target_notional=target_notional,
            actual_notional=notional,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=lev,
            margin_used=margin_req,
            liquidation_price=liq_price,
            entry_fee=entry_fee,
            accumulated_funding=0.0,
            last_funding_bucket=ts_utc // 28800,
            opened_at_utc=ts_utc,
            strategy=strategy,
            category=category,
            selection_bucket=selection_bucket,
            route_reason=route_reason,
            mark_price=init_mark,
        )
        pos.update_mark(init_mark)
        self.positions[symbol] = pos
        return True, "filled", pos

    def apply_funding_if_due(
        self,
        symbol: str,
        funding_rate: float,
        mark_price: float,
        ts_utc: int,
    ) -> float | None:
        """Apply 8h funding settlement for an open FUTURES position if boundary crossed."""
        if self.ledger_type != "FUTURES":
            return None
        pos = self.positions.get(symbol)
        if pos is None or mark_price <= 0:
            return None
        bucket = ts_utc // 28800
        if bucket <= pos.last_funding_bucket:
            return None

        buckets_elapsed = bucket - pos.last_funding_bucket
        notional_at_mark = pos.qty * mark_price
        # Positive funding rate: LONG pays, SHORT receives
        direction = 1.0 if pos.side == "LONG" else -1.0
        funding_cost = direction * notional_at_mark * float(funding_rate) * buckets_elapsed

        self.cash_usdt -= funding_cost
        self.total_funding += funding_cost
        self.realized_pnl -= funding_cost
        pos.accumulated_funding += funding_cost
        pos.last_funding_bucket = bucket
        return funding_cost

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
        if bid <= 0 or ask <= 0 or ask < bid:
            # Restore position if invalid exit quote
            self.positions[symbol] = pos
            return None

        slip = slippage_bps / 10_000.0
        exit_price = bid * (1.0 - slip) if pos.side == "LONG" else ask * (1.0 + slip)
        if exit_price <= 0:
            self.positions[symbol] = pos
            return None

        if pos.side == "LONG":
            gross_pnl = (exit_price - pos.entry_price) * pos.qty
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.qty

        exit_notional = pos.qty * exit_price
        exit_fee = exit_notional * self.fee_rate
        self.cash_usdt += pos.margin_used + gross_pnl - exit_fee
        self.total_fees += exit_fee
        self.realized_pnl += gross_pnl - exit_fee

        net_pnl = gross_pnl - pos.entry_fee - exit_fee - pos.accumulated_funding
        record = {
            "symbol": symbol,
            "ledger": self.ledger_type,
            "side": pos.side,
            "qty": pos.qty,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "target_notional": pos.target_notional,
            "actual_notional": pos.actual_notional,
            "exit_notional": exit_notional,
            "gross_pnl": gross_pnl,
            "entry_fee": pos.entry_fee,
            "exit_fee": exit_fee,
            "fee": pos.entry_fee + exit_fee,
            "funding_paid": pos.accumulated_funding,
            "net_pnl": net_pnl,
            "leverage": pos.leverage,
            "margin_used": pos.margin_used,
            "reason": reason,
            "opened_at_utc": pos.opened_at_utc,
            "closed_at_utc": ts_utc,
            "strategy": pos.strategy,
            "category": pos.category,
            "selection_bucket": pos.selection_bucket,
            "route_reason": pos.route_reason,
        }
        self.closed_trades.append(record)
        return record

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "ledger_type": self.ledger_type,
            "starting_capital": self.starting_capital,
            "cash_usdt": self.cash_usdt,
            "daily_start_equity": self.daily_start_equity,
            "trading_day": self.trading_day,
            "daily_halted": self.daily_halted,
            "daily_dd_halt_pct": self.daily_dd_halt_pct,
            "max_leverage": self.max_leverage,
            "maintenance_margin_rate": self.maintenance_margin_rate,
            "max_risk_per_trade_pct": self.max_risk_per_trade_pct,
            "fee_rate": self.fee_rate,
            "realized_pnl": self.realized_pnl,
            "total_fees": self.total_fees,
            "total_funding": self.total_funding,
            "positions": {sym: pos.to_dict() for sym, pos in self.positions.items()},
            "closed_trades": list(self.closed_trades),
        }

    @classmethod
    def from_state_dict(cls, d: dict[str, Any]) -> PaperLedger:
        ledger = cls(
            ledger_type=d["ledger_type"],
            starting_capital=float(d["starting_capital"]),
            daily_dd_halt_pct=float(d["daily_dd_halt_pct"]),
            max_leverage=float(d["max_leverage"]),
            maintenance_margin_rate=float(d["maintenance_margin_rate"]),
            max_risk_per_trade_pct=float(d["max_risk_per_trade_pct"]),
            fee_rate=float(d["fee_rate"]),
            trading_day=str(d["trading_day"]),
        )
        ledger.cash_usdt = float(d["cash_usdt"])
        ledger.daily_start_equity = float(d["daily_start_equity"])
        ledger.daily_halted = bool(d["daily_halted"])
        ledger.realized_pnl = float(d["realized_pnl"])
        ledger.total_fees = float(d["total_fees"])
        ledger.total_funding = float(d["total_funding"])
        ledger.positions = {
            sym: PaperPosition.from_dict(p) for sym, p in d.get("positions", {}).items()
        }
        ledger.closed_trades = list(d.get("closed_trades", []))
        return ledger


class GlobalPortfolioCoordinator:
    """Enforces 100-slot limit, 5000 USDT global gross exposure cap, and no duplicate symbols."""

    def __init__(
        self,
        spot_ledger: PaperLedger,
        futures_ledger: PaperLedger,
        max_concurrent_positions: int = 100,
        global_max_notional: float = 5000.0,
        target_notional: float = 50.0,
        min_tolerance_notional: float = 45.0,
        max_tolerance_notional: float = 55.0,
    ) -> None:
        self.spot = spot_ledger
        self.futures = futures_ledger
        self.max_concurrent_positions = int(max_concurrent_positions)
        self.global_max_notional = float(global_max_notional)
        self.target_notional = float(target_notional)
        self.min_tolerance_notional = float(min_tolerance_notional)
        self.max_tolerance_notional = float(max_tolerance_notional)
        self._open_lock = threading.RLock()

    def has_open_symbol(self, symbol: str) -> bool:
        return symbol in self.spot.positions or symbol in self.futures.positions

    def total_open_positions(self) -> int:
        return len(self.spot.positions) + len(self.futures.positions)

    def total_exposure(self) -> float:
        return self.spot.current_exposure() + self.futures.current_exposure()

    def remaining_exposure_capacity(self) -> float:
        return max(0.0, self.global_max_notional - self.total_exposure())

    def try_open(
        self,
        route_ledger: LedgerType,
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
        selection_bucket: str = "",
        route_reason: str = "",
        mark_price: float | None = None,
    ) -> tuple[bool, str, PaperPosition | None]:
        with self._open_lock:
            if self.has_open_symbol(symbol):
                return False, "duplicate_symbol_exposure", None
            if self.total_open_positions() >= self.max_concurrent_positions:
                return False, "max_100_positions_reached", None

            remaining_cap = self.remaining_exposure_capacity()
            if remaining_cap < self.min_tolerance_notional:
                return False, "global_exposure_cap_reached", None

            if route_ledger not in ("SPOT", "FUTURES"):
                return False, "invalid_market_route", None
            target_ledger = self.spot if route_ledger == "SPOT" else self.futures
            eff_lev = 1.0 if route_ledger == "SPOT" else leverage

            return target_ledger.open_position(
            symbol=symbol,
            side=side,
            bid=bid,
            ask=ask,
            stop_loss=stop_loss,
            take_profit=take_profit,
            rules=rules,
            ts_utc=ts_utc,
            target_notional=self.target_notional,
            min_tolerance_notional=self.min_tolerance_notional,
            max_tolerance_notional=self.max_tolerance_notional,
            max_allowed_notional=remaining_cap,
            leverage=eff_lev,
            slippage_bps=slippage_bps,
            strategy=strategy,
            category=category,
            selection_bucket=selection_bucket,
            route_reason=route_reason,
            mark_price=mark_price,
            )
