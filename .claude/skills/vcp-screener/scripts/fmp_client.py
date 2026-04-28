#!/usr/bin/env python3
"""
FMP API Client for VCP Screener

Provides rate-limited access to Financial Modeling Prep API endpoints
for VCP (Volatility Contraction Pattern) screening.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- Batch quote support
- S&P 500 constituents fetching

Data source fallback chain (Apr 2026):
- /api/v3/* deprecated Aug 31 2025 — kept as last resort for grandfathered users.
- /stable/* is the post-deprecation home, but FMP gates ~10–20 percent of mega-caps
  (e.g. AVGO, LLY, BRK.B) behind 402 even on the free tier. For those, fall through
  to Alpaca's /v2/stocks/{symbol}/bars (paper account credentials), which is
  unrestricted across SP100. The screener consumes a v3-shaped dict, so we wrap
  every response back into {symbol, historical: [{date, open, high, low, close,
  volume, ...}, ...]} regardless of source.
"""

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: requests library not found. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)


# --- FMP endpoint fallback: stable (new users) -> v3 (legacy users) ---


def _stable_quote_url(base, symbols_str, params):
    """stable/quote?symbol=^GSPC (single symbol only on free tier)"""
    params["symbol"] = symbols_str
    return base, params


def _v3_quote_url(base, symbols_str, params):
    """api/v3/quote/^GSPC (legacy users only post-Aug-2025)"""
    return f"{base}/{symbols_str}", params


def _stable_hist_url(base, symbols_str, params):
    """stable/historical-price-eod/full?symbol=^GSPC (single symbol only on free tier)

    Note: this endpoint replaces the deprecated /api/v3/historical-price-full
    after Aug 31 2025. Returns a FLAT array of bars; the caller wraps it
    back into the v3 {symbol, historical: [...]} shape for backwards compat.
    """
    params["symbol"] = symbols_str
    return base, params


def _v3_hist_url(base, symbols_str, params):
    """api/v3/historical-price-full/^GSPC (legacy users only post-Aug-2025)"""
    return f"{base}/{symbols_str}", params


_FMP_ENDPOINTS = {
    "quote": [
        ("https://financialmodelingprep.com/stable/quote", _stable_quote_url),
        ("https://financialmodelingprep.com/api/v3/quote", _v3_quote_url),
    ],
    "historical": [
        ("https://financialmodelingprep.com/stable/historical-price-eod/full", _stable_hist_url),
        ("https://financialmodelingprep.com/api/v3/historical-price-full", _v3_hist_url),
    ],
}


class FMPClient:
    """Client for Financial Modeling Prep API with rate limiting and caching"""

    BASE_URL = "https://financialmodelingprep.com/api/v3"
    RATE_LIMIT_DELAY = 0.3  # 300ms between requests

    _ENDPOINT_FAILURE_THRESHOLD = 3  # disable endpoint after N consecutive failures

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("FMP_API_KEY")
        if not self.api_key:
            raise ValueError(
                "FMP API key required. Set FMP_API_KEY environment variable "
                "or pass api_key parameter."
            )
        self.session = requests.Session()
        self.session.headers.update({"apikey": self.api_key})
        self.cache = {}
        self.last_call_time = 0
        self.rate_limit_reached = False
        self.retry_count = 0
        self.max_retries = 1
        self.api_calls_made = 0
        # Circuit breaker: track consecutive failures per endpoint URL prefix
        self._endpoint_failures: dict[str, int] = {}
        self._disabled_endpoints: set[str] = set()

    def _rate_limited_get(
        self, url: str, params: Optional[dict] = None, quiet: bool = False
    ) -> Optional[dict]:
        if self.rate_limit_reached:
            return None

        if params is None:
            params = {}

        elapsed = time.time() - self.last_call_time
        if elapsed < self.RATE_LIMIT_DELAY:
            time.sleep(self.RATE_LIMIT_DELAY - elapsed)

        try:
            response = self.session.get(url, params=params, timeout=30)
            self.last_call_time = time.time()
            self.api_calls_made += 1

            if response.status_code == 200:
                self.retry_count = 0
                return response.json()
            elif response.status_code == 429:
                self.retry_count += 1
                if self.retry_count <= self.max_retries:
                    print("WARNING: Rate limit exceeded. Waiting 60 seconds...", file=sys.stderr)
                    time.sleep(60)
                    return self._rate_limited_get(url, params, quiet=quiet)
                else:
                    print("ERROR: Daily API rate limit reached.", file=sys.stderr)
                    self.rate_limit_reached = True
                    return None
            else:
                if not quiet:
                    print(
                        f"ERROR: API request failed: {response.status_code} - {response.text[:200]}",
                        file=sys.stderr,
                    )
                return None
        except requests.exceptions.RequestException as e:
            print(f"ERROR: Request exception: {e}", file=sys.stderr)
            return None

    def _request_with_fallback(self, endpoint_key, symbols_str, extra_params=None):
        """Try stable endpoint first, fall back to v3 for legacy users.

        Returns parsed JSON in v3-compatible shape, or None if all fail.
        Non-last endpoints use quiet=True to suppress expected 403 stderr.
        """
        params = dict(extra_params) if extra_params else {}
        endpoints = _FMP_ENDPOINTS[endpoint_key]
        is_single = "," not in symbols_str

        for i, (base_url, url_builder) in enumerate(endpoints):
            # Circuit breaker: skip endpoints with too many consecutive failures
            if base_url in self._disabled_endpoints:
                continue

            url, final_params = url_builder(base_url, symbols_str, dict(params))
            is_last = i == len(endpoints) - 1
            data = self._rate_limited_get(url, final_params, quiet=not is_last)
            if not data:  # falsy (None, [], {}) — try next endpoint
                self._record_endpoint_failure(base_url)
                continue

            # Shape validation: reject truthy-but-wrong-shape responses
            valid = True
            if endpoint_key == "quote":
                if not isinstance(data, list) or len(data) == 0:
                    valid = False
                elif is_single and not any(
                    q.get("symbol", "").replace("-", ".") == symbols_str.replace("-", ".")
                    for q in data
                ):
                    valid = False

            if endpoint_key == "historical":
                # /stable/historical-price-eod/full returns a flat list of bars:
                #   [{symbol, date, open, high, low, close, volume, ...}, ...]
                # /api/v3/historical-price-full (legacy) returns a dict:
                #   {symbol, historical: [{date, open, ...}, ...]}
                # Callers expect the v3 dict shape, so wrap stable's flat list.
                if isinstance(data, list):
                    if not data:
                        valid = False
                    else:
                        norm_request = symbols_str.replace("-", ".")
                        first_sym = data[0].get("symbol", "")
                        if first_sym and first_sym.replace("-", ".") != norm_request:
                            valid = False
                        else:
                            wrapped = {
                                "symbol": first_sym or symbols_str,
                                "historical": data,
                            }
                            self._endpoint_failures[base_url] = 0
                            return wrapped
                elif not isinstance(data, dict):
                    valid = False
                elif "historicalStockList" in data:
                    # legacy batch format -> v3 single format (exact match only)
                    norm = symbols_str.replace("-", ".")
                    found = None
                    for entry in data["historicalStockList"]:
                        if entry.get("symbol", "").replace("-", ".") == norm:
                            found = {
                                "symbol": entry.get("symbol"),
                                "historical": entry.get("historical", []),
                            }
                            break
                    if found:
                        self._endpoint_failures[base_url] = 0
                        return found
                    valid = False
                elif "historical" not in data:
                    valid = False
                elif is_single and data.get("symbol"):
                    if data["symbol"].replace("-", ".") != symbols_str.replace("-", "."):
                        valid = False

            if valid:
                self._endpoint_failures[base_url] = 0
                return data
            self._record_endpoint_failure(base_url)
        return None

    def _record_endpoint_failure(self, base_url: str) -> None:
        """Track consecutive failures and disable endpoint after threshold."""
        failures = self._endpoint_failures.get(base_url, 0) + 1
        self._endpoint_failures[base_url] = failures
        if failures >= self._ENDPOINT_FAILURE_THRESHOLD:
            self._disabled_endpoints.add(base_url)

    def get_sp500_constituents(self) -> Optional[list[dict]]:
        """Fetch S&P 500 constituent list.

        Tries the post-Aug-2025 stable endpoint first, then the legacy v3
        endpoint for users with a grandfathered subscription.

        Returns:
            List of dicts with keys: symbol, name, sector, subSector
            or None on failure.
        """
        cache_key = "sp500_constituents"
        if cache_key in self.cache:
            return self.cache[cache_key]

        endpoints = (
            ("https://financialmodelingprep.com/stable/sp500-constituent", False),
            (f"{self.BASE_URL}/sp500_constituent", True),  # last → loud on error
        )
        for url, is_last in endpoints:
            data = self._rate_limited_get(url, quiet=not is_last)
            if isinstance(data, list) and data:
                self.cache[cache_key] = data
                return data
        return None

    def get_quote(self, symbols: str) -> Optional[list[dict]]:
        """Fetch real-time quote data for one or more symbols (comma-separated)"""
        cache_key = f"quote_{symbols}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        data = self._request_with_fallback("quote", symbols)
        if data:
            self.cache[cache_key] = data
        return data

    def get_historical_prices(self, symbol: str, days: int = 365) -> Optional[dict]:
        """Fetch historical daily OHLCV data.

        Source order:
          1. FMP /stable/historical-price-eod/full
          2. FMP /api/v3/historical-price-full (legacy users)
          3. Alpaca /v2/stocks/{symbol}/bars (covers FMP-gated mega-caps).

        Always returns the v3-shaped {symbol, historical: [bars...]} dict
        with bars sorted most-recent-first. None if all sources fail.
        """
        cache_key = f"prices_{symbol}_{days}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        data = self._request_with_fallback("historical", symbol, {"timeseries": days})
        if not data:
            data = self._alpaca_historical_bars(symbol, days)
        if data:
            self.cache[cache_key] = data
        return data

    def _alpaca_historical_bars(self, symbol: str, days: int) -> Optional[dict]:
        """Pull daily bars from Alpaca and reshape into the v3 historical envelope.

        Used as a fall-through when FMP gates a symbol (BRK.B, AVGO, LLY, ...).
        Requires ALPACA_API_KEY / ALPACA_SECRET_KEY env vars; if missing or the
        request fails, returns None so the caller treats the symbol as missing.
        """
        key = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        data_url = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
        if not key or not secret:
            return None
        # ~1.5x calendar buffer to cover weekends + holidays in `days` trading bars.
        end_dt = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=int(days * 1.5) + 7)
        url = f"{data_url}/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": "1Day",
            "start": start_dt.isoformat().replace("+00:00", "Z"),
            "end": end_dt.isoformat().replace("+00:00", "Z"),
            "limit": min(int(days * 1.5) + 10, 10000),
            "adjustment": "raw",
        }
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            self.api_calls_made += 1
            if r.status_code != 200:
                return None
            payload = r.json()
        except (requests.exceptions.RequestException, ValueError):
            return None

        bars = payload.get("bars") or []
        if not bars:
            return None
        # Alpaca returns oldest-first; FMP/v3 callers expect most-recent-first.
        bars_sorted = sorted(bars, key=lambda b: b.get("t", ""), reverse=True)
        historical = [
            {
                "date": (b.get("t") or "")[:10],
                "open": b.get("o", 0),
                "high": b.get("h", 0),
                "low": b.get("l", 0),
                "close": b.get("c", 0),
                "adjClose": b.get("c", 0),
                "volume": b.get("v", 0),
                "vwap": b.get("vw", 0),
                "_source": "alpaca",
            }
            for b in bars_sorted
        ]
        return {"symbol": symbol, "historical": historical}

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch quotes for a list of symbols.

        FMP free-tier `/stable/quote` only accepts one symbol per call
        (comma-separated lists return 402 Premium) and gates ~10-20 percent
        of mega-cap symbols (e.g. AVGO, LLY, BRK.B) behind a 402 paywall.
        For symbols that fail there, fall back to deriving the quote-shaped
        record from `/stable/historical-price-eod/full`, which is open on
        the free tier for the entire SP100 universe.

        Legacy `/api/v3/quote/A,B,C` (grandfathered subscriptions) does
        support batching but is unreachable from this account.
        """
        results = {}
        for symbol in symbols:
            quotes = self.get_quote(symbol)
            if quotes:
                for q in quotes:
                    results[q["symbol"]] = q
                continue
            synth = self._synthesize_quote_from_history(symbol)
            if synth:
                results[synth["symbol"]] = synth
        return results

    def _synthesize_quote_from_history(self, symbol: str) -> Optional[dict]:
        """Derive a quote-shaped dict from historical bars for symbols
        that the /stable/quote endpoint gates behind a paywall.

        Returns a dict with the fields pre_filter_stock and analyze_stock
        consume: symbol, price, yearHigh, yearLow, avgVolume, volume,
        previousClose. marketCap is left at 0 since historical bars don't
        carry it; downstream consumers that use marketCap should treat 0
        as "unknown" (none of them currently use it as a hard gate).
        """
        hist_resp = self.get_historical_prices(symbol, days=260)
        if not hist_resp:
            return None
        bars = hist_resp.get("historical") or []
        if not bars:
            return None
        latest = bars[0]
        # Most-recent-first ordering, take last 252 trading days for 52w window
        window = bars[:252]
        closes = [b.get("close", 0) for b in window if b.get("close")]
        volumes = [b.get("volume", 0) for b in window if b.get("volume")]
        if not closes:
            return None
        return {
            "symbol": symbol,
            "name": symbol,
            "price": latest.get("close", 0),
            "open": latest.get("open", 0),
            "dayHigh": latest.get("high", 0),
            "dayLow": latest.get("low", 0),
            "yearHigh": max(closes),
            "yearLow": min(closes),
            "volume": latest.get("volume", 0),
            "avgVolume": sum(volumes) / len(volumes) if volumes else 0,
            "previousClose": bars[1].get("close", latest.get("close", 0)) if len(bars) > 1 else latest.get("close", 0),
            "marketCap": 0,
            "exchange": "",
            "timestamp": 0,
            "_synthesized": True,
        }

    def get_batch_historical(self, symbols: list[str], days: int = 260) -> dict[str, list[dict]]:
        """Fetch historical prices for multiple symbols"""
        results = {}
        for symbol in symbols:
            data = self.get_historical_prices(symbol, days=days)
            if data and "historical" in data:
                results[symbol] = data["historical"]
        return results

    def calculate_sma(self, prices: list[float], period: int) -> float:
        """Calculate Simple Moving Average from a list of prices (most recent first)"""
        if len(prices) < period:
            return sum(prices) / len(prices)
        return sum(prices[:period]) / period

    def get_api_stats(self) -> dict:
        return {
            "cache_entries": len(self.cache),
            "api_calls_made": self.api_calls_made,
            "rate_limit_reached": self.rate_limit_reached,
        }
