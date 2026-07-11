# intraday_strategy.py — OPENING RANGE BREAKOUT (15-min ORB)
# ================================================================
# Replaces the old pivot_strategy.py entirely (pivot bounce/rejection
# logic removed).
#
# Logic:
#   1. Capture the High/Low of the first 15 minutes of the session,
#      i.e. 9:15–9:30 — the first three 5-min candles of the day.
#   2. Any candle AFTER that window which:
#        - OPENS above the opening-range High, and
#        - CLOSES green (close > open)
#      is a BUY breakout.
#      Mirror for SELL: OPENS below the opening-range Low and CLOSES
#      red (close < open).
#   3. Volume confirmation: the breakout candle's volume must be
#      greater than the immediately preceding candle's volume.
#
# NOTE ON CANDLE INTERVAL: this system's scanner/backtest/live engine
# only ever fetches 5-minute and 15-minute historical candles (see
# ultimate_scanner.py — every kite.historical_data() call requests
# "5minute" or "15minute", never "3minute"). Strategy functions like
# the ones below only ever receive whatever df/ind the caller fetched.
# So "the breakout candle" here is evaluated on the 5-min bar that is
# actually fed to this function — the opening-range window (first 15
# min = first three 5-min candles) is unaffected by that, since 15
# minutes is exactly three 5-min bars either way. If genuine 3-minute
# granularity is required for the breakout confirmation candle itself,
# the scanner would need to be extended to also pull a "3minute" feed
# for each symbol — that's a larger change outside this file.
# ================================================================

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# --------------- HELPERS ---------------
def _fv(ind_row, key, fallback=np.nan):
    try:
        v = float(ind_row[key])
        return fallback if np.isnan(v) else v
    except Exception:
        return fallback

def _get_opening_range(df):
    """
    Returns (or_high, or_low, bars_today) for the first 15 minutes of
    today's session (9:15–9:30 = first three 5-min candles), using
    today's bars up to and including the last row of `df`.

    bars_today = number of today's bars available so far (including the
    current/last one) — used by callers to confirm the opening-range
    window has fully formed (bars_today >= 4, i.e. at least one candle
    exists after the 9:15–9:30 window) before evaluating a breakout.

    Falls back to the first 3 rows of `df` (no date-based session
    slicing) if there's no usable date/index info.
    """
    try:
        if 'date' in df.columns:
            dates = pd.to_datetime(df['date'])
        elif isinstance(df.index, pd.DatetimeIndex):
            dates = pd.Series(df.index, index=df.index)
        else:
            if len(df) < 4:
                return None
            or_high = float(df['high'].iloc[:3].max())
            or_low = float(df['low'].iloc[:3].min())
            return or_high, or_low, len(df)

        today = dates.iloc[-1].date()
        today_mask = dates.dt.date == today
        today_bars = df[today_mask]
        if len(today_bars) < 4:
            return None

        or_bars = today_bars.iloc[:3]
        or_high = float(or_bars['high'].max())
        or_low = float(or_bars['low'].min())
        return or_high, or_low, len(today_bars)
    except Exception as e:
        logger.debug(f"opening range computation error: {e}")
        return None

# --------------- STRATEGIES ---------------
def orb_15min_buy(df, ind):
    """
    OPENING RANGE BREAKOUT (15-min) — Bullish breakout.

    9:15–9:30 High/Low captured from the first three 5-min candles.
    A later candle that OPENS above that High and CLOSES green, on
    volume higher than the previous candle, is a BUY.
    """
    try:
        if len(df) < 5:
            return False

        orr = _get_opening_range(df)
        if orr is None:
            return False
        or_high, or_low, bars_today = orr
        if bars_today < 4:
            # opening-range window hasn't fully formed yet, or this
            # candle IS part of the opening range itself
            return False

        o_now = float(df['open'].iloc[-1])
        c_now = float(df['close'].iloc[-1])
        v_now = float(df['volume'].iloc[-1])
        v_prev = float(df['volume'].iloc[-2])

        breakout_above = o_now > or_high
        green_candle = c_now > o_now
        volume_confirm = v_now > v_prev

        if breakout_above and green_candle and volume_confirm:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            logger.info(
                f"ORB_15MIN_BUY: {sym} open={o_now:.2f} > OR_high={or_high:.2f} "
                f"close={c_now:.2f} vol={v_now:.0f} > prev_vol={v_prev:.0f}"
            )
        return breakout_above and green_candle and volume_confirm
    except Exception as e:
        logger.error(f"ORB_15MIN_BUY error: {e}")
        return False

def orb_15min_sell(df, ind):
    """
    OPENING RANGE BREAKOUT (15-min) — Bearish breakdown.

    9:15–9:30 High/Low captured from the first three 5-min candles.
    A later candle that OPENS below that Low and CLOSES red, on
    volume higher than the previous candle, is a SELL.
    """
    try:
        if len(df) < 5:
            return False

        orr = _get_opening_range(df)
        if orr is None:
            return False
        or_high, or_low, bars_today = orr
        if bars_today < 4:
            return False

        o_now = float(df['open'].iloc[-1])
        c_now = float(df['close'].iloc[-1])
        v_now = float(df['volume'].iloc[-1])
        v_prev = float(df['volume'].iloc[-2])

        breakdown_below = o_now < or_low
        red_candle = c_now < o_now
        volume_confirm = v_now > v_prev

        if breakdown_below and red_candle and volume_confirm:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            logger.info(
                f"ORB_15MIN_SELL: {sym} open={o_now:.2f} < OR_low={or_low:.2f} "
                f"close={c_now:.2f} vol={v_now:.0f} > prev_vol={v_prev:.0f}"
            )
        return breakdown_below and red_candle and volume_confirm
    except Exception as e:
        logger.error(f"ORB_15MIN_SELL error: {e}")
        return False

# --------------- REGISTRY ---------------
all_strategies = {
    'ORB_15MIN_BUY':  orb_15min_buy,
    'ORB_15MIN_SELL': orb_15min_sell,
}

# --------------- MIN-BAR REQUIREMENT ---------------
# Read generically by BacktestEngine.run() (ultimate_scanner.py) to decide
# how many bars of df_slice must accumulate before it starts evaluating this
# strategy set bar-by-bar. Both ORB functions above only ever look at:
#   - today's first 3 bars (opening range, via _get_opening_range), and
#   - the current bar + 1 previous bar (for the volume-confirmation check).
# There is no EMA/indicator lookback dependency here, so this can be tiny —
# unlike v4_high_trust, which needs ~160 bars for its longer EMA/Donchian
# windows. A single NSE session is ~75 five-minute bars, so a low value here
# is what lets a single-day backtest actually evaluate any bars at all.
# If BacktestEngine can't find this attribute on a strategy module (e.g. an
# older module that predates this convention) it falls back to 160.
MIN_BARS_REQUIRED = 5

# --------------- METADATA ---------------
# See v4_high_trust.py's strategy_meta docstring for field meanings.
# category='breakout' so the scanner's regime filter (skip breakout
# strategies while the market is RANGING) applies here too.
strategy_meta = {
    'ORB_15MIN_BUY':  {'direction': 'BUY', 'category': 'breakout'},
    'ORB_15MIN_SELL': {'direction': 'SELL', 'category': 'breakout'},
}