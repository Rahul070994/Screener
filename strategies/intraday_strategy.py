# intraday_strategy.py — Simple First-Candle Opening Range Breakout (ORB) v4
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
#      - VOLUME: the breakout candle's volume must be STRICTLY GREATER
#        than the 1st candle's volume, for both BUY and SELL. A breakout
#        on volume that's lower than (or equal to) the opening candle is
#        treated as unconfirmed/likely noise and no signal fires.
#      - RANGE CAP: the breakout candle's own full range (high - low),
#        as a % of its close, must NOT exceed MAX_BREAKOUT_RANGE_PCT
#        (0.5% by default). A candle wider than this is treated as a
#        spike/outlier print rather than a genuine breakout, and no
#        signal fires.
#      - ANTI-CHASE CAP: how far price has ALREADY moved from the day's
#        OPEN (not the first candle's high/low — the actual 09:15 open
#        print) by the time the breakout candle closes must not exceed
#        MAX_EXTENSION_FROM_OPEN_PCT (0.75% by default). e.g. if the
#        stock opened at 10475 and has already run 1-2% by the time a
#        "breakout" triggers, most of the move is likely already spent
#        and the odds of a reversal/pullback from here are much higher
#        than taking a fresh breakout — so no signal fires.
#
# No EMA/RSI/ATR filters — deliberately minimal.
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

# ultimate_scanner.py now builds the strategy window with
# _session_anchored_window(), which always keeps every bar from the start
# of TODAY'S session (not just a trailing MIN_BARS_REQUIRED-sized slice) —
# so _session_start_idx() below reliably finds the true 09:15 candle no
# matter how far into the day it's called. MIN_BARS_REQUIRED here only
# needs to cover this strategy's own minimum ("at least a 1st + a
# breakout candle exist") plus a small buffer — it is NOT what limits how
# late in the day a signal can fire; MONITOR_CUTOFF_TIME does that.
#
# IMPORTANT: don't inflate this to "cover the whole monitoring window" —
# the engine won't evaluate a symbol at all until MIN_BARS_REQUIRED bars
# exist, so an oversized value here delays the FIRST check of the day
# past MONITOR_CUTOFF_TIME and the strategy can never fire. (This is
# exactly the bug in the previous version of this file — MIN_BARS_REQUIRED
# was set to 90 to work around the old trailing-window issue, which meant
# the engine's first-ever check of the day landed at 13:42, already past
# the 13:00 cutoff.)
MONITOR_CUTOFF_TIME = "13:00"  # HH:MM, 24h — no NEW ORB signal after this time
MIN_BARS_REQUIRED = 5

# Sanity cap on the breakout candle itself: its full range (high - low),
# as a percentage of its own close price, must not exceed this. Guards
# against firing on a single wild/outlier candle (a spike, a fat-finger
# print, a news-driven gap-through) where the "breakout" is really just
# noise/volatility rather than a controlled directional move.
MAX_BREAKOUT_RANGE_PCT = 0.5  # percent, e.g. 0.5 = 0.5%

# Anti-chase cap: how far price is allowed to have already moved from the
# DAY'S OPEN (the first candle's open) by the time the breakout candle
# closes. If price has already run further than this in the breakout's
# direction, the move is considered "already extended" — most of the
# room has likely been used up and the odds of a pullback/reversal from
# here are much higher than taking a fresh breakout. No signal fires in
# that case, even if every other condition (color/volume/range) is met.
MAX_EXTENSION_FROM_OPEN_PCT = 0.75  # percent, e.g. 0.75 = 0.75%

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
    required_cols = ('open', 'high', 'low', 'close', 'volume')
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
        first_open = float(first_candle['open'])
        breakout_close = float(breakout_candle['close'])
        breakout_high = float(breakout_candle['high'])
        breakout_low = float(breakout_candle['low'])
        first_volume = float(first_candle['volume'])
        breakout_volume = float(breakout_candle['volume'])
    except Exception:
        return False

    # Volume confirmation: the candle doing the breaking must trade with
    # more volume than the first (opening-range) candle — a breakout on
    # thin volume is far more likely to be noise/a false break than a
    # genuine move. Applies to both BUY and SELL.
    if breakout_volume <= first_volume:
        return False

    # Range sanity check: reject the breakout candle if its own full
    # range (high - low) is unusually wide relative to its price — this
    # is a single-candle spike/outlier, not a controlled breakout, and
    # tends to mean-revert or represent a bad/thin print rather than a
    # genuine move worth following.
    if breakout_close <= 0:
        return False
    breakout_range_pct = (breakout_high - breakout_low) / breakout_close * 100
    if breakout_range_pct > MAX_BREAKOUT_RANGE_PCT:
        return False

    # Anti-chase check: how far has price already run from the DAY'S
    # OPEN by the time this breakout candle closes? e.g. stock opens at
    # 10475 and by the breakout candle's close it's already 1-2% away —
    # most of the move is likely already used up, and odds of a
    # pullback/reversal from here are meaningfully higher than taking a
    # fresh breakout. If the extension exceeds the cap, skip the signal
    # even though every other condition was met.
    if first_open <= 0:
        return False
    extension_pct = (breakout_close - first_open) / first_open * 100
    if abs(extension_pct) > MAX_EXTENSION_FROM_OPEN_PCT:
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
            first_open = float(df.iloc[session_start]['open'])
            first_vol = float(df.iloc[session_start]['volume'])
            close_now = float(df['close'].iloc[-1])
            vol_now = float(df['volume'].iloc[-1])
            high_now = float(df['high'].iloc[-1])
            low_now = float(df['low'].iloc[-1])
            range_pct = (high_now - low_now) / close_now * 100 if close_now else 0.0
            ext_pct = (close_now - first_open) / first_open * 100 if first_open else 0.0
            logger.info(
                f"ORB_BUY: {sym} broke first-candle high={first_high:.2f} "
                f"with close={close_now:.2f}, volume={vol_now:.0f} "
                f"(1st candle volume={first_vol:.0f}), range={range_pct:.2f}%, "
                f"extension_from_open={ext_pct:.2f}%"
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
            first_open = float(df.iloc[session_start]['open'])
            first_vol = float(df.iloc[session_start]['volume'])
            close_now = float(df['close'].iloc[-1])
            vol_now = float(df['volume'].iloc[-1])
            high_now = float(df['high'].iloc[-1])
            low_now = float(df['low'].iloc[-1])
            range_pct = (high_now - low_now) / close_now * 100 if close_now else 0.0
            ext_pct = (close_now - first_open) / first_open * 100 if first_open else 0.0
            logger.info(
                f"ORB_SELL: {sym} broke first-candle low={first_low:.2f} "
                f"with close={close_now:.2f}, volume={vol_now:.0f} "
                f"(1st candle volume={first_vol:.0f}), range={range_pct:.2f}%, "
                f"extension_from_open={ext_pct:.2f}%"
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