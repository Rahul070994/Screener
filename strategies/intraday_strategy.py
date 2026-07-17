# intraday_strategy.py — Simple First-Candle Opening Range Breakout (ORB) v1
#
# Setup:
#   1. Mark the HIGH and LOW of the first 3-minute candle of the day.
#   2. Look only at the 2nd candle of the day (the very next bar).
#      - BUY  if the 2nd candle CLOSES above the 1st candle's HIGH.
#      - SELL if the 2nd candle CLOSES below the 1st candle's LOW.
#   3. Extra confirmation:
#      - BUY  requires BOTH the 1st candle and the 2nd (breakout) candle
#        to be GREEN (close > open).
#      - SELL requires BOTH the 1st candle and the 2nd (breakout) candle
#        to be RED (close < open).
#
# No EMA/RSI/volume/ATR filters — deliberately minimal.

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# --- Core parameters --------------------------------------------------------
TIMEFRAME = "3minute"
# Only need the first candle of the day + the breakout (2nd) candle, but we
# keep a small buffer so the session-start lookup always has both bars
# available inside the trimmed window the scanner passes to strategy funcs.
MIN_BARS_REQUIRED = 5

# --- Risk management (used only if something calls get_sl_target) ----------
RISK_REWARD_RATIO = 2.0

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


def _orb_signal(df, want_bullish):
    if len(df) < 2:
        return False
    required_cols = ('open', 'high', 'low', 'close')
    if not all(c in df.columns for c in required_cols):
        return False

    session_start = _session_start_idx(df)
    last_idx = len(df) - 1

    # Only fire on the 2nd candle of the session (the breakout candle).
    # Anything before or after that is not a valid signal bar for this setup.
    if last_idx != session_start + 1:
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


# --- SL/Target calculation ---------------------------------------------
def get_sl_target(df, ind, side, entry_price):
    """
    Simple SL/Target for this setup:
      BUY  -> SL = first candle's LOW,  Target = entry + risk * RISK_REWARD_RATIO
      SELL -> SL = first candle's HIGH, Target = entry - risk * RISK_REWARD_RATIO
    Falls back to a small fixed % if the first candle's range is unusable.
    """
    session_start = _session_start_idx(df)
    try:
        first_candle = df.iloc[session_start]
        first_high = float(first_candle['high'])
        first_low = float(first_candle['low'])
    except Exception:
        first_high = entry_price * 1.005
        first_low = entry_price * 0.995

    if side.upper() == 'BUY':
        sl = first_low
        risk = entry_price - sl
        if risk <= 0:
            risk = entry_price * 0.005
            sl = entry_price - risk
        target = entry_price + risk * RISK_REWARD_RATIO
    else:  # SELL
        sl = first_high
        risk = sl - entry_price
        if risk <= 0:
            risk = entry_price * 0.005
            sl = entry_price + risk
        target = entry_price - risk * RISK_REWARD_RATIO

    return float(sl), float(target)


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