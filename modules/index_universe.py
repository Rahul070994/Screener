# ============================================================
# modules/index_universe.py
# ------------------------------------------------------------
# Dynamic index-constituent lists (e.g. "which stocks are in NIFTY 200
# right now"), so this doesn't have to live as a hand-typed list in
# ultimate_scanner.py that silently goes stale every time NSE
# reconstitutes the index (twice a year, plus ad-hoc replacements).
#
# WHY THIS ISN'T FETCHED FROM KITE:
# Kite Connect has no endpoint for index membership. It only gives you
# quotes/OHLC/historical-data for a tradingsymbol you already know, and
# an instruments dump of every tradable instrument — neither tells you
# "which of these belong to NIFTY 200". This is confirmed directly by
# Zerodha staff on the Kite Connect developer forum: "Kiteconnect API
# doesn't provide fundamental related data. You may download it from
# the nse website." (https://kite.trade/forum/discussion/13633)
#
# So the actual authoritative, machine-readable, free source is NSE's
# own published index constituent CSV. This module fetches that,
# caches it to disk, and refreshes it periodically — instead of the
# app relying on a list frozen in source code.
# ============================================================

import io
import json
import logging
import os
import time

import pandas as pd
import requests

logger = logging.getLogger(__name__)

NSE_INDEX_CSV_URLS = {
    "NIFTY200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY50":  "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}

# NSE's archive server rejects requests that don't look like a browser,
# and its CSVs are only reliably served to a session that's already
# picked up cookies from a prior visit to the main site.
_NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/vnd.ms-excel,*/*",
    "Referer": "https://www.nseindia.com/market-data/live-equity-market",
}

# NSE reconstitutes these lists a handful of times a year at most, so
# checking once a day is more than enough — this is not a "prices
# change every second" kind of freshness requirement.
CACHE_MAX_AGE_SECONDS = 24 * 60 * 60


def _fetch_nse_symbol_list(index_key, timeout=10):
    """Fetch + parse the constituent CSV for `index_key` straight from
    NSE. Returns a list of trading symbols. Raises on any failure —
    the caller (get_index_symbols) decides what fallback to use."""
    url = NSE_INDEX_CSV_URLS[index_key]
    session = requests.Session()
    session.headers.update(_NSE_HEADERS)
    # Warm-up hit: primes cookies NSE's archive server checks for before
    # it will serve the CSV to a fresh session.
    session.get("https://www.nseindia.com", timeout=timeout)
    resp = session.get(url, timeout=timeout)
    resp.raise_for_status()

    df = pd.read_csv(io.StringIO(resp.text))
    symbol_col = next((c for c in df.columns if c.strip().lower() == "symbol"), None)
    if symbol_col is None:
        raise ValueError(
            f"'Symbol' column not found in NSE CSV for {index_key}; "
            f"got columns {list(df.columns)}"
        )
    symbols = [str(s).strip() for s in df[symbol_col].dropna().tolist() if str(s).strip()]
    if not symbols:
        raise ValueError(f"NSE CSV for {index_key} parsed to an empty symbol list")
    return symbols


def get_index_symbols(index_key, cache_dir, fallback_symbols=None, force_refresh=False):
    """
    Returns the current constituent list for `index_key` (e.g. "NIFTY200").

    Preference order:
      1. Fresh disk cache (younger than CACHE_MAX_AGE_SECONDS), unless
         force_refresh=True.
      2. A live fetch from NSE's official CSV (cached to disk on success).
      3. A stale disk cache — better than nothing, since index changes
         are infrequent; a several-day-old list is still ~correct.
      4. `fallback_symbols`, if provided — a last-resort safety net so
         the app can still start and scan *something* on a machine with
         no internet access and no pre-existing cache. This is NOT the
         source of truth; it only fires when NSE is unreachable AND
         there is no cache at all.

    Raises only if NSE fetch fails, there's no cache, and no
    fallback_symbols were given.
    """
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, f"{index_key.lower()}_constituents.json")

    cached = None
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                cached = json.load(f)
        except Exception as e:
            logger.warning(f"Index universe cache for {index_key} unreadable, ignoring: {e}")
            cached = None

    if not force_refresh and cached and (time.time() - cached.get("fetched_at", 0)) < CACHE_MAX_AGE_SECONDS:
        return cached["symbols"]

    try:
        symbols = _fetch_nse_symbol_list(index_key)
        with open(cache_file, "w") as f:
            json.dump({"symbols": symbols, "fetched_at": time.time()}, f)
        logger.info(f"Index universe '{index_key}': fetched {len(symbols)} symbols live from NSE")
        return symbols
    except Exception as e:
        logger.warning(f"Index universe '{index_key}': live NSE fetch failed ({e})")
        if cached and cached.get("symbols"):
            age_hrs = (time.time() - cached.get("fetched_at", 0)) / 3600
            logger.warning(
                f"Index universe '{index_key}': using stale cache "
                f"({age_hrs:.1f}h old, {len(cached['symbols'])} symbols)"
            )
            return cached["symbols"]
        if fallback_symbols:
            logger.warning(
                f"Index universe '{index_key}': no cache available either — falling back "
                f"to built-in {len(fallback_symbols)}-symbol safety-net list"
            )
            return list(fallback_symbols)
        raise