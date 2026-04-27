#!/usr/bin/env python3
"""
FMP API Client for Earnings Trade Analyzer

Provides rate-limited access to Financial Modeling Prep API endpoints
for post-earnings trade analysis and scoring.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- API call budget enforcement
- Batch profile support
- Earnings calendar fetching
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


# --- FMP endpoint fallback: stable (current) -> v3 (legacy, deprecated 2025-08-31) ---
#
# As of 2025-08-31, FMP retired /api/v3/* for non-legacy keys. The /stable/*
# replacements moved historical bars to /historical-price-eod/full and changed
# the response shape from {symbol, historical: [...]} (v3) to a flat list of
# bars [{symbol, date, ...}] (stable). We still try v3 last for legacy keys.


def _stable_hist_url(base, symbols_str, params):
    """stable/historical-price-eod/full?symbol=SPY&timeseries=90 (flat-list response)"""
    params["symbol"] = symbols_str
    return base, params


def _v3_hist_url(base, symbols_str, params):
    """api/v3/historical-price-full/SPY?timeseries=90 (legacy users only)"""
    return f"{base}/{symbols_str}", params


_FMP_ENDPOINTS = {
    "historical": [
        ("https://financialmodelingprep.com/stable/historical-price-eod/full", _stable_hist_url),
        ("https://financialmodelingprep.com/api/v3/historical-price-full", _v3_hist_url),
    ],
}


class ApiCallBudgetExceeded(Exception):
    """Raised when the API call budget has been exhausted."""

    pass


class FMPClient:
    """Client for Financial Modeling Prep API with rate limiting, caching, and budget control"""

    BASE_URL = "https://financialmodelingprep.com/api/v3"
    RATE_LIMIT_DELAY = 0.3  # 300ms between requests
    US_EXCHANGES = ["NYSE", "NASDAQ", "AMEX", "NYSEArca", "BATS", "NMS", "NGM", "NCM"]

    _ENDPOINT_FAILURE_THRESHOLD = 3  # disable endpoint after N consecutive failures

    def __init__(self, api_key: Optional[str] = None, max_api_calls: int = 200):
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
        self.max_api_calls = max_api_calls
        # Circuit breaker: track consecutive failures per endpoint URL prefix
        self._endpoint_failures: dict[str, int] = {}
        self._disabled_endpoints: set[str] = set()

    def _rate_limited_get(
        self, url: str, params: Optional[dict] = None, quiet: bool = False
    ) -> Optional[dict]:
        """Execute a rate-limited GET request with budget enforcement."""
        if self.rate_limit_reached:
            return None

        if self.api_calls_made >= self.max_api_calls:
            raise ApiCallBudgetExceeded(
                f"API call budget exceeded: {self.api_calls_made}/{self.max_api_calls} calls used."
            )

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
        except requests.exceptions.Timeout:
            print(f"WARNING: Request timed out for {url}", file=sys.stderr)
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
            if not data:  # falsy (None, [], {}) -- try next endpoint
                self._record_endpoint_failure(base_url)
                continue

            # Shape validation: reject truthy-but-wrong-shape responses
            valid = True
            if endpoint_key == "historical":
                # /stable/historical-price-eod/full returns a flat list:
                #   [{symbol, date, open, high, low, close, volume, change, ...}, ...]
                # /api/v3/historical-price-full returns a dict:
                #   {symbol, historical: [{date, open, ...}, ...]}
                # Adapt both to v3-shape `{symbol, historical: [...]}` for callers.
                if isinstance(data, list):
                    norm = symbols_str.replace("-", ".")
                    if is_single and data and data[0].get("symbol"):
                        if data[0]["symbol"].replace("-", ".") != norm:
                            valid = False
                    if valid:
                        self._endpoint_failures[base_url] = 0
                        # Strip per-row symbol to match v3 historical row shape
                        rows = [{k: v for k, v in row.items() if k != "symbol"} for row in data]
                        return {"symbol": symbols_str, "historical": rows}
                elif not isinstance(data, dict):
                    valid = False
                elif "historicalStockList" in data:
                    # legacy stable batch format -> v3 single format (exact match only)
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

    def get_earnings_calendar(self, from_date: str, to_date: str) -> Optional[list]:
        """Fetch earnings calendar for a date range.

        Args:
            from_date: Start date (YYYY-MM-DD)
            to_date: End date (YYYY-MM-DD)

        Returns:
            List of earnings announcements with v3-compatible field names
            (eps, epsEstimated, revenue, revenueEstimated, time, symbol, date).
            The /stable/* endpoint dropped the bmo/amc `time` field; we leave
            it absent so callers' normalize_timing() returns "unknown".
        """
        cache_key = f"earnings_{from_date}_{to_date}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        url = "https://financialmodelingprep.com/stable/earnings-calendar"
        params = {"from": from_date, "to": to_date}
        data = self._rate_limited_get(url, params)
        if not isinstance(data, list):
            return None

        # Field rename: stable's epsActual/revenueActual -> v3's eps/revenue.
        adapted = []
        for row in data:
            adapted.append({
                "symbol": row.get("symbol"),
                "date": row.get("date"),
                "eps": row.get("epsActual"),
                "epsEstimated": row.get("epsEstimated"),
                "revenue": row.get("revenueActual"),
                "revenueEstimated": row.get("revenueEstimated"),
                # `time` (bmo/amc) absent in /stable/earnings-calendar; passes
                # through to normalize_timing() as "unknown".
            })
        self.cache[cache_key] = adapted
        return adapted

    def get_company_profiles(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch company profiles, one /stable/profile call per symbol.

        Args:
            symbols: List of ticker symbols

        Returns:
            Dict mapping symbol to profile data with v3-compatible aliases:
            mktCap (= stable.marketCap) and exchangeShortName (= stable.exchange).

        Note: /stable/profile accepts only one symbol per call on the free tier.
        Budget impact: O(N) calls vs the v3 batch endpoint's O(1).
        """
        profiles: dict[str, dict] = {}

        for symbol in symbols:
            cache_key = f"profile_{symbol}"
            if cache_key in self.cache:
                cached = self.cache[cache_key]
                if cached:
                    profiles[symbol] = cached
                continue

            url = "https://financialmodelingprep.com/stable/profile"
            data = self._rate_limited_get(url, {"symbol": symbol}, quiet=True)
            if isinstance(data, list) and data:
                profile = dict(data[0])
                # Alias stable -> v3 field names so call sites don't change
                if "marketCap" in profile and "mktCap" not in profile:
                    profile["mktCap"] = profile["marketCap"]
                if "exchange" in profile and "exchangeShortName" not in profile:
                    profile["exchangeShortName"] = profile["exchange"]
                profiles[profile.get("symbol", symbol)] = profile
                self.cache[cache_key] = profile
            else:
                self.cache[cache_key] = None

        return profiles

    def get_historical_prices(self, symbol: str, days: int = 250) -> Optional[list[dict]]:
        """Fetch historical daily OHLCV data for a symbol.

        Args:
            symbol: Ticker symbol
            days: Number of trading days to fetch (default: 250)

        Returns:
            List of price dicts (most-recent-first) or None on failure.
        """
        cache_key = f"prices_{symbol}_{days}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        data = self._request_with_fallback("historical", symbol, {"timeseries": days})
        if data and "historical" in data:
            result = data["historical"]
            self.cache[cache_key] = result
            return result
        return None

    def get_api_stats(self) -> dict:
        """Return API usage statistics."""
        return {
            "cache_entries": len(self.cache),
            "api_calls_made": self.api_calls_made,
            "max_api_calls": self.max_api_calls,
            "rate_limit_reached": self.rate_limit_reached,
        }
