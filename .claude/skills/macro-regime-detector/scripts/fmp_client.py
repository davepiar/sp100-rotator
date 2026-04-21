#!/usr/bin/env python3
"""
FMP API Client for Macro Regime Detector

Provides rate-limited access to Financial Modeling Prep API endpoints
for macro regime detection analysis.

Features:
- Rate limiting (0.3s between requests)
- Automatic retry on 429 errors
- Session caching for duplicate requests
- Batch historical data support
- Treasury rates endpoint support
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


class FMPClient:
    """Client for Financial Modeling Prep API with rate limiting and caching"""

    BASE_URL = "https://financialmodelingprep.com/api/v3"
    STABLE_URL = "https://financialmodelingprep.com/stable"
    RATE_LIMIT_DELAY = 0.3  # 300ms between requests

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

    def _rate_limited_get(self, url: str, params: Optional[dict] = None) -> Optional[dict]:
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
                    return self._rate_limited_get(url, params)
                else:
                    print("ERROR: Daily API rate limit reached.", file=sys.stderr)
                    self.rate_limit_reached = True
                    return None
            else:
                print(
                    f"ERROR: API request failed: {response.status_code} - {response.text[:200]}",
                    file=sys.stderr,
                )
                return None
        except requests.exceptions.RequestException as e:
            print(f"ERROR: Request exception: {e}", file=sys.stderr)
            return None

    def get_historical_prices(self, symbol: str, days: int = 600) -> Optional[dict]:
        """Fetch historical daily OHLCV data. FMP stable endpoint first; Alpaca fallback if FMP 402s."""
        cache_key = f"prices_{symbol}_{days}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        url = f"{self.STABLE_URL}/historical-price-eod/full"
        params = {"symbol": symbol}
        data = self._rate_limited_get(url, params)
        if isinstance(data, list) and data:
            wrapped = {"symbol": symbol, "historical": data[:days]}
            self.cache[cache_key] = wrapped
            return wrapped
        if isinstance(data, dict) and "historical" in data:
            self.cache[cache_key] = data
            return data

        # Fallback: Alpaca bars
        ak = os.getenv("ALPACA_API_KEY")
        sk = os.getenv("ALPACA_SECRET_KEY")
        durl = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
        if not (ak and sk):
            return None
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc) - timedelta(minutes=20)
        start = end - timedelta(days=int(days * 1.6) + 10)
        bars = []
        page_token = None
        while True:
            p = {
                "symbols": symbol,
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
                    print(f"  Alpaca fallback for {symbol}: HTTP {r.status_code}", file=sys.stderr)
                    return None
                j = r.json()
                b = j.get("bars", {}).get(symbol, [])
                bars.extend(b)
                page_token = j.get("next_page_token")
                if not page_token:
                    break
            except Exception as e:
                print(f"  Alpaca fallback for {symbol} exception: {e}", file=sys.stderr)
                return None
        if not bars:
            return None
        # Alpaca returns oldest-first. FMP returns newest-first. Match FMP.
        historical = [
            {
                "date": b["t"][:10],
                "open": b["o"],
                "high": b["h"],
                "low": b["l"],
                "close": b["c"],
                "volume": b["v"],
            }
            for b in reversed(bars)
        ][:days]
        wrapped = {"symbol": symbol, "historical": historical}
        self.cache[cache_key] = wrapped
        return wrapped

    def get_batch_historical(self, symbols: list[str], days: int = 600) -> dict[str, list[dict]]:
        """Fetch historical prices for multiple symbols"""
        results = {}
        for symbol in symbols:
            data = self.get_historical_prices(symbol, days=days)
            if data and "historical" in data:
                results[symbol] = data["historical"]
        return results

    def get_treasury_rates(self, days: int = 600) -> Optional[list[dict]]:
        """
        Fetch treasury rate data from FMP stable endpoint.

        Returns list of dicts with keys like 'date', 'year2', 'year10', etc.
        Most recent first.
        """
        cache_key = f"treasury_{days}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        url = f"{self.STABLE_URL}/treasury-rates"
        params = {"limit": days}
        data = self._rate_limited_get(url, params)
        if data and isinstance(data, list):
            self.cache[cache_key] = data
            return data
        return None

    def get_api_stats(self) -> dict:
        return {
            "cache_entries": len(self.cache),
            "api_calls_made": self.api_calls_made,
            "rate_limit_reached": self.rate_limit_reached,
        }
