# intraday_strategy.py — Simple First-Candle Opening Range Breakout (ORB) v2
#
# Setup:
#   1. Mark the HIGH and LOW of the first 3-minute candle of the day
#      (09:15–09:18).
#   2. From then on, watch EVERY candle (not just the next one) up to
#      MONITOR_CUTOFF_TIME (13:00):
#      - BUY  if that candle CLOSES above the 1st candle's HIGH.
#      - SELL if that candle CLOSES below the 1st candle's LOW.
#      No new signal is generated once the candle's time is past
#      MONITOR_CUTOFF_TIME.
#   3. Extra confirmation:
#      - BUY  requires BOTH the 1st candle and the breakout candle to be
#        GREEN (close > open).
#      - SELL requires BOTH the 1st candle and the breakout candle to be
#        RED (close < open).
#
# No EMA/RSI/volume/ATR filters — deliberately minimal.
#
# SL/Target: this strategy does NOT define its own SL/Target logic.
# The scanner's flat target_pct / stoploss_pct (Settings → Target & Stop
# Loss) is used instead, same as every other strategy.

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# --- Core parameters --------------------------------------------------------
TIMEFRAME = "3minute"

# The scanner trims the fetched historical data down to the last
# MIN_BARS_REQUIRED bars before handing it to strategy functions (see
# ultimate_scanner.py: `df_w = df.iloc[-min_bars_needed:]`). _session_start_idx
# below finds "the first candle of today" by scanning for the oldest bar in
# that trimmed window that still belongs to today — so if this value is too
# small, the true 09:15 candle scrolls out of the window as the day goes on,
# and the "first candle" the strategy sees quietly becomes some other,
# WRONG candle. This is what was silently corrupting the high/low reference
# (and therefore any SL/Target derived from it) after the first ~15 minutes
# of the session.
#
# We need the window to comfortably cover 09:15 -> MONITOR_CUTOFF_TIME on a
# 3-minute timeframe: (13:00 - 09:15) = 225 minutes / 3 = 75 candles.
# Add a generous buffer for holidays/half-days/missing bars.
MONITOR_CUTOFF_TIME = "13:00"  # HH:MM, 24h — no NEW ORB signal after this time
MIN_BARS_REQUIRED = 90

# ----------------------------------------------------------------------------

def _session_start_idx(df):
    """Index of the first bar belonging to the same calendar day as the
    last bar in df (i.e. the first 3-min candle of the current session)."""
    if 'date' not in df.columns or len(df) == 0:
        return 0
    try:
        dates = pd.to_datetime(df['date'])
        last_date = dates.iloc[-1].date()
        same_day = dates.dt.date.values == last_date
        idxs = np.flatnonzero(same_day)
        return int(idxs[0]) if len(idxs) else 0
    except Exception:
        return 0


def _is_green(row):
    return float(row['close']) > float(row['open'])


def _is_red(row):
    return float(row['close']) < float(row['open'])


def _within_monitor_window(df, last_idx):
    """True if the last candle's time-of-day is at or before
    MONITOR_CUTOFF_TIME. No 'date' column -> fail open (allow), same as
    the rest of this file's defensive fallbacks."""
    if 'date' not in df.columns:
        return True
    try:
        cutoff_h, cutoff_m = (int(x) for x in MONITOR_CUTOFF_TIME.split(':'))
        ts = pd.to_datetime(df.iloc[last_idx]['date'])
        return (ts.hour, ts.minute) <= (cutoff_h, cutoff_m)
    except Exception:
        return True


def _orb_signal(df, want_bullish):
    if len(df) < 2:
        return False
    required_cols = ('open', 'high', 'low', 'close')
    if not all(c in df.columns for c in required_cols):
        return False

    session_start = _session_start_idx(df)
    last_idx = len(df) - 1

    # Any candle AFTER the first candle of the day is a valid breakout
    # candle — not just the one immediately following it.
    if last_idx <= session_start:
        return False

    # No new signal once we're past the monitoring cutoff.
    if not _within_monitor_window(df, last_idx):
        return False

    first_candle = df.iloc[session_start]
    breakout_candle = df.iloc[last_idx]

    try:
        first_high = float(first_candle['high'])
        first_low = float(first_candle['low'])
        breakout_close = float(breakout_candle['close'])
    except Exception:
        return False

    if want_bullish:
        if not (_is_green(first_candle) and _is_green(breakout_candle)):
            return False
        return breakout_close > first_high
    else:
        if not (_is_red(first_candle) and _is_red(breakout_candle)):
            return False
        return breakout_close < first_low


# --- Entry functions ---------------------------------------------------
def orb_buy(df, ind=None):
    try:
        signal = _orb_signal(df, want_bullish=True)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            session_start = _session_start_idx(df)
            first_high = float(df.iloc[session_start]['high'])
            close_now = float(df['close'].iloc[-1])
            logger.info(
                f"ORB_BUY: {sym} broke first-candle high={first_high:.2f} "
                f"with close={close_now:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"ORB_BUY error: {e}")
        return False


def orb_sell(df, ind=None):
    try:
        signal = _orb_signal(df, want_bullish=False)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            session_start = _session_start_idx(df)
            first_low = float(df.iloc[session_start]['low'])
            close_now = float(df['close'].iloc[-1])
            logger.info(
                f"ORB_SELL: {sym} broke first-candle low={first_low:.2f} "
                f"with close={close_now:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"ORB_SELL error: {e}")
        return False


# --- Metadata for scanner ---------------------------------------------------
# No diagnostics/exit functions registered — this setup is intentionally
# minimal (entry-only; exits are handled purely by target/SL/EOD squareoff).
strategy_diagnostics = {}

strategy_exits = {}

all_strategies = {
    'ORB_BUY': orb_buy,
    'ORB_SELL': orb_sell,
}

strategy_meta = {
    'ORB_BUY': {'direction': 'BUY', 'category': 'breakout', 'skip_quality_checks': True},
    'ORB_SELL': {'direction': 'SELL', 'category': 'breakout', 'skip_quality_checks': True},
}