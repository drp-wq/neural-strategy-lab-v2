"""Binance PUBLIC market-data collectors for V2 (Spot + USDT-M Futures, no live endpoints)."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import requests

from v2.ledger import SymbolRules

STABLECOINS = {"USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "USDE", "USD1", "RLUSD"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
DEFI_BASES = {
    "UNI", "AAVE", "LINK", "MKR", "CRV", "LDO", "SNX", "COMP", "SUSHI", "RUNE",
    "ENA", "PENDLE", "JUP", "CAKE", "1INCH", "DYDX", "GMX", "MORPHO", "SKY",
}
MAJORS_BASES = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "TRX", "LTC", "TON", "SUI"}

ALLOWED_PUBLIC_PATHS = (
    "/api/v3/exchangeInfo",
    "/api/v3/ticker/24hr",
    "/api/v3/klines",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/klines",
)


class TokenBucketRateLimiter:
    """Thread-safe token-bucket rate limiter with dynamic Binance weight throttling."""

    def __init__(self, rate_per_sec: float = 12.0, burst: int = 12, weight_soft_cap: int = 4200) -> None:
        self.rate = max(1.0, float(rate_per_sec))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.weight_soft_cap = int(weight_soft_cap)
        self.last_used_weight = 0
        self._last_ts = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_ts
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self._last_ts = now
                if self.last_used_weight >= self.weight_soft_cap:
                    wait = 2.0
                elif self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                else:
                    wait = (1.0 - self.tokens) / self.rate
            time.sleep(min(wait, 2.0))

    def observe_weight(self, used_weight: int | None) -> None:
        if used_weight is None:
            return
        with self._lock:
            self.last_used_weight = max(0, int(used_weight))


@dataclass
class MarketQuote:
    market: str  # "SPOT" | "FUTURES"
    symbol: str
    bid: float
    ask: float
    last_price: float
    mark_price: float
    volume_24h_usdt: float
    price_change_pct: float
    funding_rate: float
    rules: SymbolRules
    ts_utc: int
    onboard_date_ms: int | None = None


@dataclass
class UnifiedMarketSymbol:
    symbol: str
    base_asset: str
    category: str
    selection_bucket: str
    tags: list[str] = field(default_factory=list)
    spot: MarketQuote | None = None
    futures: MarketQuote | None = None
    is_verified_new_listing: bool = False
    onboard_date_ms: int | None = None

    @property
    def best_volume_24h_usdt(self) -> float:
        return max(
            self.spot.volume_24h_usdt if self.spot else 0.0,
            self.futures.volume_24h_usdt if self.futures else 0.0,
        )

    @property
    def price_change_pct(self) -> float:
        if self.futures is not None:
            return self.futures.price_change_pct
        if self.spot is not None:
            return self.spot.price_change_pct
        return 0.0

    def primary_market_for_candles(self) -> str:
        if self.futures is not None and (
            self.spot is None or self.futures.volume_24h_usdt >= self.spot.volume_24h_usdt
        ):
            return "FUTURES"
        return "SPOT"


def parse_exchange_rules(sym_info: dict[str, Any]) -> SymbolRules | None:
    """Extract LOT_SIZE and NOTIONAL/MIN_NOTIONAL filters from Binance exchangeInfo."""
    filters = {f.get("filterType"): f for f in sym_info.get("filters", []) if isinstance(f, dict)}
    lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
    if not lot:
        return None
    min_qty = float(lot.get("minQty", 0.0) or 0.0)
    max_qty = float(lot.get("maxQty", 1e12) or 1e12)
    step_size = float(lot.get("stepSize", 0.0) or 0.0)
    if step_size <= 0 and min_qty <= 0:
        return None

    notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    min_notional = float(
        notional_filter.get("minNotional") or notional_filter.get("notional") or 5.0
    )
    max_notional = float(notional_filter.get("maxNotional") or 1e12)
    return SymbolRules(
        min_qty=min_qty,
        step_size=step_size,
        min_notional=min_notional,
        max_qty=max_qty,
        max_notional=max_notional,
        source="exchangeInfo",
    )


def validate_quote(q: MarketQuote | None, now_utc: int, stale_sec: int, max_spread_pct: float = 0.005) -> tuple[bool, str]:
    """Fail closed on missing, stale, crossed, zero, or wide-spread quotes."""
    if q is None:
        return False, "missing_quote"
    if (now_utc - q.ts_utc) > stale_sec:
        return False, "stale_quote"
    if q.bid <= 0 or q.ask <= 0 or q.mark_price <= 0 or q.last_price <= 0:
        return False, "invalid_zero_price"
    if q.ask < q.bid:
        return False, "crossed_book"
    mid = (q.bid + q.ask) / 2.0
    spread = (q.ask - q.bid) / mid
    if spread > max_spread_pct:
        return False, "spread_too_wide"
    # Sanity check mark vs mid (>15% divergence indicates corrupt feed)
    if abs(q.mark_price - mid) / mid > 0.15:
        return False, "invalid_mark_divergence"
    return True, "ok"


class PublicBinanceV2Collector:
    """Rate-limit-safe public Spot + USDT-M Futures collector (read-only public endpoints)."""

    def __init__(
        self,
        stale_sec: int = 120,
        timeout_sec: int = 10,
        max_retries: int = 3,
        exchange_info_ttl_sec: int = 6 * 3600,
        requests_per_second: float = 12.0,
        spot_weight_soft_cap: int = 4200,
        futures_weight_soft_cap: int = 1600,
        session: requests.Session | None = None,
    ) -> None:
        self.stale_sec = int(stale_sec)
        self.timeout_sec = int(timeout_sec)
        self.max_retries = int(max_retries)
        self.exchange_info_ttl_sec = int(exchange_info_ttl_sec)
        self.session = session or requests.Session()
        self.logger = logging.getLogger("neural_lab_v2")

        self._spot_limiter = TokenBucketRateLimiter(
            rate_per_sec=requests_per_second, burst=10, weight_soft_cap=spot_weight_soft_cap
        )
        self._fut_limiter = TokenBucketRateLimiter(
            rate_per_sec=requests_per_second, burst=10, weight_soft_cap=futures_weight_soft_cap
        )
        self._spot_rules_cache: dict[str, tuple[SymbolRules, str]] = {}
        self._fut_rules_cache: dict[str, tuple[SymbolRules, int | None, str]] = {}
        self._cache_ts_spot: int = 0
        self._cache_ts_fut: int = 0

    def _get_json(self, market: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if path not in ALLOWED_PUBLIC_PATHS:
            raise ValueError(f"Blocked non-public endpoint path: {path}")
        mkt = market.upper()
        base = "https://api.binance.com" if mkt == "SPOT" else "https://fapi.binance.com"
        limiter = self._spot_limiter if mkt == "SPOT" else self._fut_limiter
        url = f"{base}{path}"

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            limiter.acquire()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_sec)
                used_w = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
                if used_w and str(used_w).isdigit():
                    limiter.observe_weight(int(used_w))
                if resp.status_code in (418, 429):
                    retry_after = float(resp.headers.get("Retry-After", 2 * attempt))
                    self.logger.warning("Rate limited (%s) on %s, sleeping %.1fs", resp.status_code, path, retry_after)
                    time.sleep(min(retry_after, 15.0))
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(0.5 * attempt)
        raise RuntimeError(f"Public GET failed {url}: {last_exc}") from last_exc

    def refresh_exchange_info(self, now_utc: int | None = None, force: bool = False) -> None:
        now = int(time.time()) if now_utc is None else int(now_utc)
        if force or not self._spot_rules_cache or (now - self._cache_ts_spot) >= self.exchange_info_ttl_sec:
            s_info = self._get_json("SPOT", "/api/v3/exchangeInfo")
            spot_map: dict[str, tuple[SymbolRules, str]] = {}
            for item in s_info.get("symbols", []):
                sym = str(item.get("symbol", "")).upper()
                if (
                    item.get("status") == "TRADING"
                    and item.get("quoteAsset") == "USDT"
                    and item.get("isSpotTradingAllowed")
                    and sym.isascii()
                    and sym.isalnum()
                ):
                    rules = parse_exchange_rules(item)
                    if rules is not None:
                        spot_map[sym] = (rules, str(item.get("baseAsset", sym[:-4])).upper())
            self._spot_rules_cache = spot_map
            self._cache_ts_spot = now

        if force or not self._fut_rules_cache or (now - self._cache_ts_fut) >= self.exchange_info_ttl_sec:
            f_info = self._get_json("FUTURES", "/fapi/v1/exchangeInfo")
            fut_map: dict[str, tuple[SymbolRules, int | None, str]] = {}
            for item in f_info.get("symbols", []):
                sym = str(item.get("symbol", "")).upper()
                if (
                    item.get("status") == "TRADING"
                    and item.get("quoteAsset") == "USDT"
                    and item.get("contractType") == "PERPETUAL"
                    and sym.isascii()
                    and sym.isalnum()
                ):
                    rules = parse_exchange_rules(item)
                    if rules is not None:
                        onboard_raw = item.get("onboardDate")
                        onboard_ms = int(onboard_raw) if onboard_raw is not None else None
                        fut_map[sym] = (rules, onboard_ms, str(item.get("baseAsset", sym[:-4])).upper())
            self._fut_rules_cache = fut_map
            self._cache_ts_fut = now

    @staticmethod
    def _is_notional_feasible(price: float, rules: SymbolRules, max_tol: float) -> bool:
        if price <= 0:
            return False
        if rules.min_notional > max_tol:
            return False
        if rules.min_qty * price > max_tol:
            return False
        return True

    def fetch_market_quotes(
        self,
        min_vol_usdt: float = 5_000_000.0,
        max_spread_pct: float = 0.005,
        now_utc: int | None = None,
    ) -> tuple[dict[str, MarketQuote], dict[str, MarketQuote]]:
        """Fetch live Spot + Futures tickers and Futures mark/funding prices."""
        now = int(time.time()) if now_utc is None else int(now_utc)
        self.refresh_exchange_info(now_utc=now)

        spot_raw = self._get_json("SPOT", "/api/v3/ticker/24hr")
        fut_raw = self._get_json("FUTURES", "/fapi/v1/ticker/24hr")
        prem_raw = self._get_json("FUTURES", "/fapi/v1/premiumIndex")

        prem_map: dict[str, tuple[float, float]] = {}
        for p in prem_raw:
            sym = str(p.get("symbol", "")).upper()
            try:
                mp = float(p.get("markPrice") or 0.0)
                fr = float(p.get("lastFundingRate") or 0.0)
            except (TypeError, ValueError):
                mp, fr = 0.0, 0.0
            if mp > 0:
                prem_map[sym] = (mp, fr)

        spot_quotes: dict[str, MarketQuote] = {}
        for t in spot_raw:
            sym = str(t.get("symbol", "")).upper()
            if sym not in self._spot_rules_cache:
                continue
            rules, base = self._spot_rules_cache[sym]
            if base in STABLECOINS or base.endswith(LEVERAGED_SUFFIXES):
                continue
            vol = float(t.get("quoteVolume") or 0.0)
            if vol < min_vol_usdt:
                continue
            bid = float(t.get("bidPrice") or t.get("lastPrice") or 0.0)
            ask = float(t.get("askPrice") or t.get("lastPrice") or 0.0)
            last = float(t.get("lastPrice") or 0.0)
            q = MarketQuote(
                market="SPOT",
                symbol=sym,
                bid=bid,
                ask=ask,
                last_price=last,
                mark_price=last,
                volume_24h_usdt=vol,
                price_change_pct=float(t.get("priceChangePercent") or 0.0),
                funding_rate=0.0,
                rules=rules,
                ts_utc=now,
            )
            ok, _ = validate_quote(q, now_utc=now, stale_sec=self.stale_sec, max_spread_pct=max_spread_pct)
            if ok:
                spot_quotes[sym] = q

        fut_quotes: dict[str, MarketQuote] = {}
        for t in fut_raw:
            sym = str(t.get("symbol", "")).upper()
            if sym not in self._fut_rules_cache:
                continue
            rules, onboard_ms, base = self._fut_rules_cache[sym]
            if base in STABLECOINS or base.endswith(LEVERAGED_SUFFIXES):
                continue
            vol = float(t.get("quoteVolume") or 0.0)
            if vol < min_vol_usdt:
                continue
            last = float(t.get("lastPrice") or 0.0)
            bid = float(t.get("bidPrice") or last or 0.0)
            ask = float(t.get("askPrice") or last or 0.0)
            if bid <= 0 or ask <= 0:
                bid = last
                ask = last
            mark, fr = prem_map.get(sym, (last, 0.0))
            q = MarketQuote(
                market="FUTURES",
                symbol=sym,
                bid=bid,
                ask=ask,
                last_price=last,
                mark_price=mark,
                volume_24h_usdt=vol,
                price_change_pct=float(t.get("priceChangePercent") or 0.0),
                funding_rate=fr,
                rules=rules,
                ts_utc=now,
                onboard_date_ms=onboard_ms,
            )
            ok, _ = validate_quote(q, now_utc=now, stale_sec=self.stale_sec, max_spread_pct=max_spread_pct)
            if ok:
                fut_quotes[sym] = q

        return spot_quotes, fut_quotes

    def build_100_market_universe(
        self,
        spot_quotes: dict[str, MarketQuote],
        fut_quotes: dict[str, MarketQuote],
        max_symbols: int = 100,
        new_listing_window_days: int = 90,
        max_tolerance_notional: float = 55.0,
        now_utc: int | None = None,
    ) -> list[UnifiedMarketSymbol]:
        """Build category-stratified universe of up to 100 DISTINCT eligible USDT symbols."""
        now = int(time.time()) if now_utc is None else int(now_utc)
        now_ms = now * 1000
        new_window_ms = new_listing_window_days * 86400 * 1000

        all_syms = sorted(set(spot_quotes.keys()) | set(fut_quotes.keys()))
        candidates: list[UnifiedMarketSymbol] = []
        for sym in all_syms:
            sq = spot_quotes.get(sym)
            fq = fut_quotes.get(sym)
            spot_ok = sq is not None and self._is_notional_feasible(sq.ask, sq.rules, max_tolerance_notional)
            fut_ok = fq is not None and self._is_notional_feasible(fq.ask, fq.rules, max_tolerance_notional)
            if not spot_ok and not fut_ok:
                continue

            base = sym[:-4]
            onboard_ms = fq.onboard_date_ms if fq else None
            is_new = bool(onboard_ms is not None and (now_ms - onboard_ms) <= new_window_ms)

            tags: list[str] = []
            if base in MAJORS_BASES:
                tags.append("major")
            if base in DEFI_BASES:
                tags.append("defi")
            if is_new:
                tags.append("new_listing")

            base_cat = "major" if base in MAJORS_BASES else (
                "defi" if base in DEFI_BASES else ("new_listing" if is_new else "alt")
            )
            candidates.append(
                UnifiedMarketSymbol(
                    symbol=sym,
                    base_asset=base,
                    category=base_cat,
                    selection_bucket=base_cat,
                    tags=tags,
                    spot=sq if spot_ok else None,
                    futures=fq if fut_ok else None,
                    is_verified_new_listing=is_new,
                    onboard_date_ms=onboard_ms,
                )
            )

        by_vol = sorted(candidates, key=lambda c: c.best_volume_24h_usdt, reverse=True)
        by_gain = sorted(candidates, key=lambda c: c.price_change_pct, reverse=True)
        by_loss = sorted(candidates, key=lambda c: c.price_change_pct)

        majors = [c for c in by_vol if c.base_asset in MAJORS_BASES][:10]
        defis = [c for c in by_vol if c.base_asset in DEFI_BASES][:12]
        new_listings = [c for c in by_vol if c.is_verified_new_listing][:10]
        gainers = by_gain[:20]
        losers = by_loss[:20]

        selected: dict[str, UnifiedMarketSymbol] = {}

        def _add_bucket(items: list[UnifiedMarketSymbol], bucket_name: str) -> None:
            for item in items:
                if len(selected) >= max_symbols:
                    return
                if item.symbol not in selected:
                    item.selection_bucket = bucket_name
                    if bucket_name not in item.tags:
                        item.tags.append(bucket_name)
                    selected[item.symbol] = item
                else:
                    if bucket_name not in selected[item.symbol].tags:
                        selected[item.symbol].tags.append(bucket_name)

        _add_bucket(majors, "major")
        _add_bucket(defis, "defi")
        _add_bucket(new_listings, "new_listing")
        _add_bucket(gainers, "gainer")
        _add_bucket(losers, "loser")
        _add_bucket(by_vol, "high_volume")

        return list(selected.values())

    def fetch_klines(self, market: str, symbol: str, interval: str = "15m", limit: int = 60) -> pd.DataFrame:
        mkt = market.upper()
        path = "/api/v3/klines" if mkt == "SPOT" else "/fapi/v1/klines"
        raw = self._get_json(mkt, path, params={"symbol": symbol, "interval": interval, "limit": limit})
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
        df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce").astype("int64")
        return df[["open_time", "open", "high", "low", "close", "volume", "close_time"]].reset_index(drop=True)

    def fetch_klines_batch(
        self,
        items: list[tuple[str, str]],
        interval: str = "15m",
        limit: int = 60,
        max_workers: int = 6,
    ) -> dict[str, pd.DataFrame]:
        """Fetch klines concurrently with rate-limiter protection."""
        results: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futs = {
                pool.submit(self.fetch_klines, mkt, sym, interval, limit): sym
                for mkt, sym in items
            }
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    results[sym] = fut.result()
                except Exception as exc:
                    self.logger.warning("Kline fetch error %s: %s", sym, exc)
        return results
