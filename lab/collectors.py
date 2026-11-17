"""Binance PUBLIC market-data collectors for Spot and USDT-M Futures (no keys)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

STABLECOINS = {"USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDP", "EUR", "USDE"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")
DEFI_BASES = {"UNI", "AAVE", "LINK", "MKR", "CRV", "LDO", "SNX", "COMP", "SUSHI", "RUNE"}
MAJORS_BASES = {"BTC", "ETH", "BNB", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "TRX"}


@dataclass
class MarketSnapshot:
    symbol: str
    bid: float
    ask: float
    mark_price: float
    volume_24h_usdt: float
    price_change_pct: float
    category: str
    ts_utc: int


class PublicBinanceCollector:
    """Collects public Spot or USDT-M Futures market data with retry/backoff & stale checks."""

    def __init__(
        self,
        market: str = "SPOT",
        stale_sec: int = 300,
        timeout_sec: int = 10,
        max_retries: int = 3,
        session: requests.Session | None = None,
    ) -> None:
        self.market = market.upper()
        self.base_url = (
            "https://api.binance.com"
            if self.market == "SPOT"
            else "https://fapi.binance.com"
        )
        self.stale_sec = stale_sec
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self.session = session or requests.Session()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout_sec)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(0.5 * attempt)
        raise RuntimeError(f"Public GET failed {url}: {last_exc}") from last_exc

    def fetch_universe(self, max_symbols: int = 50, min_vol_usdt: float = 5_000_000.0) -> list[MarketSnapshot]:
        """Sample up to max_symbols eligible liquid USDT pairs across categories."""
        ticker_path = "/api/v3/ticker/24hr" if self.market == "SPOT" else "/fapi/v1/ticker/24hr"
        raw = self._get_json(ticker_path)
        now = int(time.time())

        eligible: list[MarketSnapshot] = []
        for t in raw:
            sym = str(t.get("symbol", "")).upper()
            if not sym.endswith("USDT"):
                continue
            base = sym[:-4]
            if base in STABLECOINS or base.endswith(LEVERAGED_SUFFIXES):
                continue

            vol = float(t.get("quoteVolume", 0.0) or 0.0)
            if vol < min_vol_usdt:
                continue

            bid = float(t.get("bidPrice") or t.get("lastPrice") or 0.0)
            ask = float(t.get("askPrice") or t.get("lastPrice") or 0.0)
            last = float(t.get("lastPrice") or 0.0)
            if bid <= 0 or ask <= 0 or last <= 0 or ask < bid:
                continue
            spread = (ask - bid) / ((ask + bid) / 2.0)
            if spread > 0.005:
                continue

            chg = float(t.get("priceChangePercent", 0.0) or 0.0)
            cat = "major" if base in MAJORS_BASES else ("defi" if base in DEFI_BASES else "alt")
            eligible.append(
                MarketSnapshot(
                    symbol=sym,
                    bid=bid,
                    ask=ask,
                    mark_price=last,
                    volume_24h_usdt=vol,
                    price_change_pct=chg,
                    category=cat,
                    ts_utc=now,
                )
            )

        # Transparent stratified sampling: top volume + gainers + losers + defi/majors
        by_vol = sorted(eligible, key=lambda x: x.volume_24h_usdt, reverse=True)
        by_gain = sorted(eligible, key=lambda x: x.price_change_pct, reverse=True)
        by_loss = sorted(eligible, key=lambda x: x.price_change_pct)

        selected: dict[str, MarketSnapshot] = {}
        for pool in (by_vol[:20], by_gain[:10], by_loss[:10], by_vol):
            for item in pool:
                if len(selected) >= max_symbols:
                    break
                selected.setdefault(item.symbol, item)
        return list(selected.values())

    def fetch_klines(self, symbol: str, interval: str = "15m", limit: int = 120) -> pd.DataFrame:
        path = "/api/v3/klines" if self.market == "SPOT" else "/fapi/v1/klines"
        raw = self._get_json(path, params={"symbol": symbol, "interval": interval, "limit": limit})
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

    def is_stale(self, snapshot: MarketSnapshot, now_utc: int | None = None) -> bool:
        now = int(time.time()) if now_utc is None else now_utc
        return (now - snapshot.ts_utc) > self.stale_sec
