# momentum_scalp_strategy.py — 3-Minute Momentum Continuation Micro-Scalp v2
#
# TIMEFRAME CHANGE NOTE (1-minute -> 3-minute):
# All the bar-count parameters below (LOOKBACK_BARS_FOR_EXTENSION,
# RVOL_LOOKBACK, EMA_FAST/EMA_SLOW, RSI_PERIOD, ATR_PERIOD) are counted in
# BARS, not minutes — they were left as-is, so each now spans 3x the real
# time it used to (e.g. LOOKBACK_BARS_FOR_EXTENSION=5 is now a 15-minute
# extension check instead of 5 minutes; RVOL_LOOKBACK=10 is now a 30-minute
# rolling volume average instead of 10 minutes). That's the standard/
# expected way to port a bar-counted setup to a slower timeframe, but call
# it out in case you want tighter lookbacks for a 3-minute chart — happy to
# re-tune the bar counts down if 15-30 minute windows feel too slow for
# what you want out of a "scalp."
#
# CHANGELOG vs v1
# ----------------
# BUG FIXES:
#   1. MAX_CANDLE_RANGE_PCT and MAX_EXTENSION_PCT were nearly equal (0.35 vs
#      0.30), so the two filters fought each other unpredictably across
#      volatility regimes. Both are now derived from ATR instead of fixed %,
#      so they self-scale per symbol and per session's volatility.
#   2. confirm candle's close (c_close) was used in the "stepping up/down"
#      comparison without a zero-guard (only s_close was guarded). Fixed.
#   3. Volume expansion was checked against a single prior candle only,
#      which one noisy tick can satisfy. Replaced with relative volume vs a
#      rolling average (RVOL), which is a much more standard and reliable
#      "is participation actually elevated" test.
#
# NEW FILTERS ADDED (all optional/togglable, all must pass):
#   - TREND FILTER (EMA 9/21): only take BUY when EMA9 > EMA21 (uptrend
#     regime), only take SELL when EMA9 < EMA21 (downtrend regime). 2-candle
#     momentum setups have a materially better hit rate *with* the prevailing
#     trend than against it or in chop.
#   - RSI(14) FILTER: rejects BUY if RSI > RSI_OVERBOUGHT (you'd be chasing
#     an already-extended move) and rejects SELL if RSI < RSI_OVERSOLD.
#   - RELATIVE VOLUME (RVOL) FILTER: signal candle's volume must be at least
#     RVOL_MULTIPLIER times the rolling average volume of the prior N bars,
#     not just bigger than the single previous candle.
#   - ATR-BASED RANGE/EXTENSION CAPS: replaces fixed % thresholds with
#     multiples of ATR(14), so the filters adapt automatically to each
#     symbol's own volatility instead of using one static number for every
#     stock.
#   - TIME-OF-DAY FILTER: skips the first few and last several minutes of
#     the session (optional — only applied if a usable timestamp column is
#     present), where spreads/volume are erratic and false signals cluster.
#
# WHAT THIS STRATEGY STILL DOES **NOT** DO:
#   - It does not predict specific future candles. Adding indicators makes
#     the *filter* stronger (fewer low-quality signals pass through), it
#     does not turn this into a forecasting model. Treat all of this as
#     signal-quality filtering, not an edge guarantee — backtest and
#     paper-trade before risking capital, and size positions accordingly.
#     This file is provided for you to evaluate and adapt; it is not
#     financial advice.
#
# SL/Target: unchanged from v1 — this file still relies on the scanner's
# flat target_pct / stoploss_pct. Consider an ATR-based SL/target instead
# of a flat %, since a flat % mismatched to a symbol's real volatility is a
# common way scalp strategies bleed on fees/slippage. Happy to add that if
# your scanner engine supports per-signal dynamic SL/target.
#
# REQUIRED COLUMNS: open, high, low, close, volume
# OPTIONAL COLUMN: a timestamp-like column (tries 'timestamp', 'date', or
# 'datetime') — if present and parseable, the time-of-day filter activates
# automatically; if absent, that filter is silently skipped (does not
# block signals).

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# --- Core parameters --------------------------------------------------------
TIMEFRAME = "3minute"  # 3-minute candles

# --- Trend filter (EMA) ------------------------------------------------
EMA_FAST = 9
EMA_SLOW = 21
USE_TREND_FILTER = True

# --- RSI filter ----------------------------------------------------------
RSI_PERIOD = 14
RSI_OVERBOUGHT = 75.0
RSI_OVERSOLD = 25.0
USE_RSI_FILTER = True

# --- Relative volume filter ---------------------------------------------
RVOL_LOOKBACK = 10          # bars used for the rolling average volume
RVOL_MULTIPLIER = 1.3       # signal candle volume must be >= 1.3x the avg
USE_RVOL_FILTER = True

# --- ATR-based adaptive range / extension caps ---------------------------
ATR_PERIOD = 14
MAX_CANDLE_RANGE_ATR_MULT = 1.2   # signal candle range must be <= 1.2x ATR
MAX_EXTENSION_ATR_MULT = 1.0      # extension from N bars back must be <= 1.0x ATR
LOOKBACK_BARS_FOR_EXTENSION = 5

# --- Minimum body (kept as a %, this one doesn't need volatility scaling
#     since a doji is a doji regardless of the symbol's ATR) -------------
MIN_BODY_PCT = 0.05  # percent

# --- Time-of-day filter ---------------------------------------------------
USE_TIME_FILTER = True
SKIP_FIRST_MINUTES = 5    # skip signals in the first N minutes after open
SKIP_LAST_MINUTES = 15    # skip signals in the last N minutes before close
MARKET_OPEN = "09:15"     # adjust to your exchange
MARKET_CLOSE = "15:30"    # adjust to your exchange

# Bars needed: longest of EMA_SLOW, RSI_PERIOD+1, ATR_PERIOD+1, RVOL_LOOKBACK,
# plus the extension lookback and a small buffer.
MIN_BARS_REQUIRED = max(EMA_SLOW, RSI_PERIOD + 1, ATR_PERIOD + 1,
                         RVOL_LOOKBACK, LOOKBACK_BARS_FOR_EXTENSION) + 2

# ----------------------------------------------------------------------------

def _is_green(row):
    return float(row['close']) > float(row['open'])


def _is_red(row):
    return float(row['close']) < float(row['open'])


def _ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def _rsi(series, period):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)  # neutral when undefined (e.g. no losses yet)


def _atr(df, period):
    high, low, close = df['high'].astype(float), df['low'].astype(float), df['close'].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _find_timestamp_col(df):
    for c in ('timestamp', 'date', 'datetime'):
        if c in df.columns:
            return c
    return None


def _passes_time_filter(df):
    if not USE_TIME_FILTER:
        return True
    ts_col = _find_timestamp_col(df)
    if ts_col is None:
        return True  # can't check, don't block
    try:
        ts = pd.to_datetime(df[ts_col].iloc[-1])
        t = ts.time()
        open_t = pd.to_datetime(MARKET_OPEN).time()
        close_t = pd.to_datetime(MARKET_CLOSE).time()
        open_dt = pd.Timestamp.combine(ts.date(), open_t)
        close_dt = pd.Timestamp.combine(ts.date(), close_t)
        cur_dt = pd.Timestamp.combine(ts.date(), t)
        mins_since_open = (cur_dt - open_dt).total_seconds() / 60
        mins_to_close = (close_dt - cur_dt).total_seconds() / 60
        if mins_since_open < SKIP_FIRST_MINUTES:
            return False
        if mins_to_close < SKIP_LAST_MINUTES:
            return False
        return True
    except Exception:
        return True  # if parsing fails, don't block on it


def _momentum_signal(df, want_bullish):
    if len(df) < MIN_BARS_REQUIRED:
        return False
    required_cols = ('open', 'high', 'low', 'close', 'volume')
    if not all(c in df.columns for c in required_cols):
        return False

    confirm = df.iloc[-2]   # second-last completed candle
    signal = df.iloc[-1]    # last completed candle

    try:
        c_open, c_close = float(confirm['open']), float(confirm['close'])
        s_open, s_high, s_low, s_close = (
            float(signal['open']), float(signal['high']),
            float(signal['low']), float(signal['close'])
        )
    except Exception:
        return False

    # Zero-guards on BOTH candles (v1 only checked s_close).
    if s_close <= 0 or c_close <= 0:
        return False

    # --- Time-of-day filter ---
    if not _passes_time_filter(df):
        return False

    # --- Minimum body check (unchanged, % based) ---
    body_pct = abs(s_close - s_open) / s_close * 100
    if body_pct < MIN_BODY_PCT:
        return False

    # --- ATR-based volatility measure ---
    atr_series = _atr(df, ATR_PERIOD)
    atr_now = float(atr_series.iloc[-1])
    if not np.isfinite(atr_now) or atr_now <= 0:
        return False

    # ATR-adaptive range cap (replaces fixed MAX_CANDLE_RANGE_PCT)
    candle_range = s_high - s_low
    if candle_range > MAX_CANDLE_RANGE_ATR_MULT * atr_now:
        return False

    # ATR-adaptive extension cap (replaces fixed MAX_EXTENSION_PCT)
    ref_idx = len(df) - 1 - LOOKBACK_BARS_FOR_EXTENSION
    if ref_idx < 0:
        return False
    try:
        ref_close = float(df.iloc[ref_idx]['close'])
    except Exception:
        return False
    if ref_close <= 0:
        return False
    extension = abs(s_close - ref_close)
    if extension > MAX_EXTENSION_ATR_MULT * atr_now:
        return False

    # --- Relative volume filter (replaces single-candle volume compare) ---
    if USE_RVOL_FILTER:
        try:
            vol_window = df['volume'].astype(float).iloc[-(RVOL_LOOKBACK + 1):-1]
            avg_vol = vol_window.mean()
            s_vol = float(signal['volume'])
        except Exception:
            return False
        if avg_vol <= 0 or s_vol < RVOL_MULTIPLIER * avg_vol:
            return False

    # --- Trend filter (EMA fast vs slow) ---
    if USE_TREND_FILTER:
        closes = df['close'].astype(float)
        ema_fast = _ema(closes, EMA_FAST).iloc[-1]
        ema_slow = _ema(closes, EMA_SLOW).iloc[-1]
        if want_bullish and not (ema_fast > ema_slow):
            return False
        if (not want_bullish) and not (ema_fast < ema_slow):
            return False

    # --- RSI filter (avoid chasing an already-extended move) ---
    if USE_RSI_FILTER:
        rsi_now = _rsi(df['close'].astype(float), RSI_PERIOD).iloc[-1]
        if want_bullish and rsi_now > RSI_OVERBOUGHT:
            return False
        if (not want_bullish) and rsi_now < RSI_OVERSOLD:
            return False

    # --- Core 2-candle stepping pattern (same logic as v1, now zero-guarded) ---
    if want_bullish:
        if not (_is_green(confirm) and _is_green(signal)):
            return False
        return s_close > c_close
    else:
        if not (_is_red(confirm) and _is_red(signal)):
            return False
        return s_close < c_close


# --- Entry functions ---------------------------------------------------
def momentum_scalp_buy(df, ind=None):
    try:
        signal = _momentum_signal(df, want_bullish=True)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            close_now = float(df['close'].iloc[-1])
            logger.info(f"MOM_SCALP_BUY_V2: {sym} confirmed with trend/RSI/RVOL/ATR filters, close={close_now:.2f}")
        return bool(signal)
    except Exception as e:
        logger.error(f"MOM_SCALP_BUY_V2 error: {e}")
        return False


def momentum_scalp_sell(df, ind=None):
    try:
        signal = _momentum_signal(df, want_bullish=False)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            close_now = float(df['close'].iloc[-1])
            logger.info(f"MOM_SCALP_SELL_V2: {sym} confirmed with trend/RSI/RVOL/ATR filters, close={close_now:.2f}")
        return bool(signal)
    except Exception as e:
        logger.error(f"MOM_SCALP_SELL_V2 error: {e}")
        return False


# --- Metadata for scanner ---------------------------------------------------
strategy_diagnostics = {}

strategy_exits = {}

all_strategies = {
    'MOM_SCALP_BUY_V2': momentum_scalp_buy,
    'MOM_SCALP_SELL_V2': momentum_scalp_sell,
}

strategy_meta = {
    'MOM_SCALP_BUY_V2': {'direction': 'BUY', 'category': 'momentum', 'skip_quality_checks': False},
    'MOM_SCALP_SELL_V2': {'direction': 'SELL', 'category': 'momentum', 'skip_quality_checks': False},
}