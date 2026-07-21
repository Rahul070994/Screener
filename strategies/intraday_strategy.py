# momentum_scalp_strategy.py — 1-Minute Momentum Continuation Micro-Scalp v1
#
# Setup:
#   1. Every time a new 1-minute candle closes, look at the last TWO
#      completed candles (the "signal candle" = last, and the "confirm
#      candle" = second-last).
#   2. BUY  if BOTH candles are green (close > open) AND each candle's
#      close is higher than the previous one's close (i.e. price is
#      stepping up, not just green-but-flat) AND volume is expanding
#      (signal candle's volume > confirm candle's volume).
#      SELL is the exact mirror (both red, stepping down, volume
#      expanding).
#   3. Extra filters (all must pass, both directions):
#      - RANGE CAP: the signal candle's own range (high-low) as a % of
#        its close must not exceed MAX_CANDLE_RANGE_PCT — rejects spike/
#        outlier prints, which is exactly the kind of single-candle noise
#        a 1-minute timeframe is prone to.
#      - MIN BODY: the signal candle's body (|close-open|) as a % of its
#        close must be at least MIN_BODY_PCT — rejects doji/indecision
#        candles that happen to close green/red by a tick.
#      - ANTI-CHASE CAP: how far price has already moved from the candle
#        N bars back (LOOKBACK_BARS_FOR_EXTENSION) must not exceed
#        MAX_EXTENSION_PCT — avoids jumping in after the move that was
#        "the next 1-2 candles" has already happened.
#
# WHAT THIS STRATEGY DOES **NOT** DO:
#   - It does not "predict" candles in any statistical/ML sense — no
#     model here can reliably forecast a specific future candle. What it
#     does is a simple, well-known heuristic: when the last 2 candles
#     already agree on direction with rising volume, the next 1-2
#     candles have a modest statistical tendency to continue that
#     direction slightly more often than not (classic momentum/
#     "candle-following" effect) — no more than that. Treat it as a
#     short-lived edge, not a forecast.
#
# SL/Target: same as intraday_strategy.py — this file does NOT define its
# own SL/Target. It relies entirely on the scanner's flat target_pct /
# stoploss_pct (Settings → Target & Stop Loss). See the message this file
# was delivered with for the specific target/SL values recommended for
# a strategy this tight (they need to clear round-trip charges even on a
# 1-2 tick move, which a generic 1%/0.5% intraday default will NOT do
# well for a scalp this fast).
#
# Continuous trading: this file has no cooldown/positions logic of its
# own — the scanner engine already enforces MAX_OPEN_POS (1 concurrent
# position) and re-scans every new candle, so once a position exits
# (target/SL hit) the very next qualifying candle re-fires a fresh
# signal automatically. Nothing extra needed here for "trade one after
# another."

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# --- Core parameters --------------------------------------------------------
TIMEFRAME = "minute"  # 1-minute candles

# Only need the last 2 completed candles for the signal itself, plus a
# small buffer for the extension lookback below. Kept small deliberately —
# see intraday_strategy.py's note on why an oversized MIN_BARS_REQUIRED
# delays the engine's first-ever check of the day.
LOOKBACK_BARS_FOR_EXTENSION = 5
MIN_BARS_REQUIRED = LOOKBACK_BARS_FOR_EXTENSION + 2

# Sanity cap on the signal candle's own range (high-low) as % of its
# close. 1-minute candles are noisy; this keeps out spike/outlier prints.
MAX_CANDLE_RANGE_PCT = 0.35  # percent

# Minimum body size (|close-open| as % of close) for the signal candle —
# rejects doji/indecision candles that closed green/red by a hair.
MIN_BODY_PCT = 0.05  # percent

# Anti-chase: how far price is allowed to have already moved from the
# candle LOOKBACK_BARS_FOR_EXTENSION bars back, by the time the signal
# candle closes. If the move is already this extended, skip — most of
# the "next 1-2 candle" move this strategy looks for has likely already
# happened.
MAX_EXTENSION_PCT = 0.30  # percent

# ----------------------------------------------------------------------------

def _is_green(row):
    return float(row['close']) > float(row['open'])


def _is_red(row):
    return float(row['close']) < float(row['open'])


def _momentum_signal(df, want_bullish):
    if len(df) < max(3, LOOKBACK_BARS_FOR_EXTENSION + 1):
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
        c_vol = float(confirm['volume'])
        s_vol = float(signal['volume'])
    except Exception:
        return False

    if s_close <= 0:
        return False

    # Volume expansion: signal candle must trade with more volume than
    # the confirm candle before it — rising participation behind the move.
    if s_vol <= c_vol:
        return False

    # Range sanity check on the signal candle.
    range_pct = (s_high - s_low) / s_close * 100
    if range_pct > MAX_CANDLE_RANGE_PCT:
        return False

    # Minimum body check on the signal candle.
    body_pct = abs(s_close - s_open) / s_close * 100
    if body_pct < MIN_BODY_PCT:
        return False

    # Anti-chase check vs N bars back.
    ref_idx = len(df) - 1 - LOOKBACK_BARS_FOR_EXTENSION
    if ref_idx < 0:
        return False
    try:
        ref_close = float(df.iloc[ref_idx]['close'])
    except Exception:
        return False
    if ref_close <= 0:
        return False
    extension_pct = (s_close - ref_close) / ref_close * 100
    if abs(extension_pct) > MAX_EXTENSION_PCT:
        return False

    if want_bullish:
        if not (_is_green(confirm) and _is_green(signal)):
            return False
        # Stepping up: signal candle's close must be higher than confirm
        # candle's close (not just green-but-flat).
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
            vol_now = float(df['volume'].iloc[-1])
            vol_prev = float(df['volume'].iloc[-2])
            logger.info(
                f"MOM_SCALP_BUY: {sym} 2-candle up-step confirmed, "
                f"close={close_now:.2f}, volume={vol_now:.0f} (prev={vol_prev:.0f})"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"MOM_SCALP_BUY error: {e}")
        return False


def momentum_scalp_sell(df, ind=None):
    try:
        signal = _momentum_signal(df, want_bullish=False)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            close_now = float(df['close'].iloc[-1])
            vol_now = float(df['volume'].iloc[-1])
            vol_prev = float(df['volume'].iloc[-2])
            logger.info(
                f"MOM_SCALP_SELL: {sym} 2-candle down-step confirmed, "
                f"close={close_now:.2f}, volume={vol_now:.0f} (prev={vol_prev:.0f})"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"MOM_SCALP_SELL error: {e}")
        return False


# --- Metadata for scanner ---------------------------------------------------
strategy_diagnostics = {}

strategy_exits = {}

all_strategies = {
    'MOM_SCALP_BUY': momentum_scalp_buy,
    'MOM_SCALP_SELL': momentum_scalp_sell,
}

strategy_meta = {
    'MOM_SCALP_BUY': {'direction': 'BUY', 'category': 'momentum', 'skip_quality_checks': True},
    'MOM_SCALP_SELL': {'direction': 'SELL', 'category': 'momentum', 'skip_quality_checks': True},
}