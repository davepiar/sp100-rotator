#!/usr/bin/env python3
"""
FMP API Client for FTD Detector

Provides rate-limited access to Financial Modeling Prep API endpoints
for follow-through day detection analysis.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- Batch quote support for ETF baskets
"""

import os
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    print("ERROR: requests library not found. Install with: pip install requests", file=sys.stderr)
    sys.exit(1)


# --- FMP endpoint fallback: stable (new users) -> v3 (legacy users) ---


def _stable_quote_url(base, symbols_str, params):
    """stable/quote?symbol=^GSPC"""
    params["symbol"] = symbols_str
    return base, params


def _v3_quote_url(base, symbols_str, params):
    """api/v3/quote/^GSPC"""
    return f"{base}/{symbols_str}", params


def _stable_hist_url(base, symbols_str, params):
    """stable/historical-price-full?symbol=^GSPC&timeseries=80"""
    params["symbol"] = symbols_str
    return base, params


def _v3_hist_url(base, symbols_str, params):
    """api/v3/historical-price-full/^GSPC?timeseries=80"""
    return f"{base}/{symbols_str}", params


_FMP_ENDPOINTS = {
    "quote": [
        ("https://financialmodelingprep.com/stable/quote", _stable_quote_url),
        ("https://financialmodelingprep.com/api/v3/quote", _v3_quote_url),
    ],
    "historical": [
        ("https://financialmodelingprep.com/stable/historical-price-full", _stable_hist_url),
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
        """Try stable endpoint first, fall back to v3 with circuit breaker."""
        params = dict(extra_params) if extra_params else {}
        endpoints = _FMP_ENDPOINTS[endpoint_key]
        is_single = "," not in symbols_str

        for i, (base_url, url_builder) in enumerate(endpoints):
            if base_url in self._disabled_endpoints:
                continue

            url, final_params = url_builder(base_url, symbols_str, dict(params))
            is_last = i == len(endpoints) - 1
            data = self._rate_limited_get(url, final_params, quiet=not is_last)
            if not data:
                self._record_endpoint_failure(base_url)
                continue

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
                if not isinstance(data, dict):
                    valid = False
                elif "historicalStockList" in data:
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
        failures = self._endpoint_failures.get(base_url, 0) + 1
        self._endpoint_failures[base_url] = failures
        if failures >= self._ENDPOINT_FAILURE_THRESHOLD:
            self._disabled_endpoints.add(base_url)

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
        """Fetch historical OHLCV. Try stable-eod first; on empty/failure, fall back to Alpaca bars."""
        cache_key = f"prices_{symbol}_{days}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Try FMP stable /historical-price-eod/full (list, newest-first)
        url = "https://financialmodelingprep.com/stable/historical-price-eod/full"
        data = self._rate_limited_get(url, {"symbol": symbol}, quiet=True)
        if isinstance(data, list) and data:
            wrapped = {"symbol": symbol, "historical": data[:days]}
            self.cache[cache_key] = wrapped
            return wrapped

        # Alpaca fallback
        ak = os.getenv("ALPACA_API_KEY")
        sk = os.getenv("ALPACA_SECRET_KEY")
        durl = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
        if not (ak and sk):
            return None
        # Map index symbols to ETF proxies
        alpaca_symbol = {"^GSPC": "SPY", "^IXIC": "QQQ", "^DJI": "DIA", "^RUT": "IWM"}.get(symbol, symbol)
        if alpaca_symbol.startswith("^"):
            return None
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc) - timedelta(minutes=20)
        start = end - timedelta(days=int(days * 1.6) + 10)
        bars = []
        page_token = None
        while True:
            p = {
                "symbols": alpaca_symbol,
                "timeframe": "1Day",
                "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "limit": 10000,
                "adjustment": "raw",
                "feed": "iex",
            }
            if page_token:
                p["page_token"] = page_token
            try:
                r = requests.get(
                    f"{durl}/v2/stocks/bars",
                    headers={"APCA-API-KEY-ID": ak, "APCA-API-SECRET-KEY": sk},
                    params=p,
                    timeout=30,
                )
                if r.status_code != 200:
                    return None
                j = r.json()
                b = j.get("bars", {}).get(alpaca_symbol, [])
                bars.extend(b)
                page_token = j.get("next_page_token")
                if not page_token:
                    break
            except Exception:
                return None
        if not bars:
            return None
        historical = [
            {
                "date": bb["t"][:10],
                "open": bb["o"],
                "high": bb["h"],
                "low": bb["l"],
                "close": bb["c"],
                "volume": bb["v"],
            }
            for bb in reversed(bars)
        ][:days]
        wrapped = {"symbol": symbol, "historical": historical}
        self.cache[cache_key] = wrapped
        return wrapped

    def get_batch_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch quotes for a list of symbols, batching up to 5 per request"""
        results = {}
        # FMP supports comma-separated symbols in quote endpoint
        batch_size = 5
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            batch_str = ",".join(batch)
            quotes = self.get_quote(batch_str)
            if quotes:
                for q in quotes:
                    results[q["symbol"]] = q
        return results

    def get_batch_historical(self, symbols: list[str], days: int = 50) -> dict[str, list[dict]]:
        """Fetch historical prices for multiple symbols"""
        results = {}
        for symbol in symbols:
            data = self.get_historical_prices(symbol, days=days)
            if data and "historical" in data:
                results[symbol] = data["historical"]
        return results

    def calculate_ema(self, prices: list[float], period: int) -> float:
        """Calculate Exponential Moving Average from a list of prices (most recent first)"""
        if len(prices) < period:
            return sum(prices) / len(prices)

        prices_reversed = prices[::-1]
        sma = sum(prices_reversed[:period]) / period
        ema = sma
        k = 2 / (period + 1)
        for price in prices_reversed[period:]:
            ema = price * k + ema * (1 - k)
        return ema

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
