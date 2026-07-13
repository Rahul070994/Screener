# confluence_strategy.py — EMA20/EMA50 Momentum Confirmation (v6)
# ================================================================
# Rewritten to match the exact spec provided:
#
#   BUY  Step 1: EMA20 crosses ABOVE EMA50 (prev EMA20<=EMA50, now EMA20>EMA50)
#        Step 2: keep monitoring every subsequent candle — no fixed limit —
#                until either the eligibility conditions below are met, or
#                EMA20 crosses back BELOW EMA50 (setup cancelled).
#        Step 3: EMA Difference % = ((EMA20 - EMA50) / EMA50) * 100 >= 0.50
#        Step 4: on that same candle, Close > EMA20 AND Close > EMA50
#                (completed-candle values only, no intrabar data)
#        Step 5: signal fires on the candle where Step 3 + Step 4 are BOTH
#                true for the first time since the Step 1 crossover; entry
#                itself happens at the next candle open via the engine's
#                normal entry mechanism (this function only returns the
#                signal, it does not place the order).
#
#   SELL is the exact mirror: EMA20 crosses BELOW EMA50, then
#        EMA Difference % = ((EMA50 - EMA20) / EMA50) * 100 >= 0.50,
#        with Close < EMA20 AND Close < EMA50 on that candle.
#
# Everything else from earlier versions (RSI filter, ADX/volatility filter,
# volume-surge filter, and the v3 direction-label inversion in
# strategy_meta) has been removed — it is not part of this spec.
#
# CALLING CONTRACT (unchanged from ultimate_scanner.py): every strategy is
# called as func(df, ind), where `ind` is the DataFrame returned by
# Indicators.calculate_all(), already sliced to MIN_BARS_REQUIRED bars,
# with 'ema_20' / 'ema_50' columns. `df` has 'close'.
#
# WINDOW LIMITATION: because the engine only ever hands this function the
# last MIN_BARS_REQUIRED bars, "no fixed candle limit" for Step 2
# monitoring is bounded in practice by that window — if the EMA20/EMA50
# crossover happened further back than MIN_BARS_REQUIRED bars ago, this
# function can no longer see it and will report "no active setup" until a
# fresh crossover happens inside the visible window.
#
# SAME-DAY (INTRADAY) BOUNDARY: ultimate_scanner.py's BacktestEngine and
# live _check_signal() both fetch one continuous multi-day "3minute"
# history per symbol and slice the last MIN_BARS_REQUIRED bars as a
# rolling window — there is NO session reset in that windowing. Early in a
# session (e.g. 10:00 AM with only ~15 bars since 9:15 open) that window
# reaches back into the PREVIOUS trading day's candles, so a naive
# backward scan for "the last crossover" can find one from yesterday's
# session and treat it as still "active" today — which is wrong for an
# intraday (MIS) strategy where every position is squared off and nothing
# carries over from one session to the next. To fix this, every scan below
# is bounded to bars that share the same calendar date as the most recent
# (current) bar in the window, using the `date` column ultimate_scanner.py
# always includes in `df` (and therefore, positionally, in `ind`). If no
# `date` column is present (defensive fallback only), scans fall back to
# the full window rather than failing closed.
# ================================================================

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

MIN_BARS_REQUIRED = 100      # EMA50 warm-up buffer (see WINDOW LIMITATION note)
EMA_DIFF_THRESHOLD_PCT = 0.50  # required |EMA20-EMA50| / EMA50 * 100 before entry


def _session_start_idx(df):
    """
    Returns the earliest positional index in `df` whose calendar date
    matches the LAST bar's calendar date — i.e. the index of the first
    candle of the current trading session inside the visible window.
    Falls back to 0 (whole window) if `date` is missing or unparsable, so
    a data-shape surprise degrades to the old (pre-fix) behavior instead
    of silently breaking the strategy.
    """
    if 'date' not in df.columns or len(df) == 0:
        return 0
    try:
        dates = pd.to_datetime(df['date'])
        last_date = dates.iloc[-1].date()
        same_day = dates.dt.date.values == last_date
        # first True index in same_day (dates are chronological, so the
        # True run is contiguous and ends at the last bar)
        idxs = np.flatnonzero(same_day)
        return int(idxs[0]) if len(idxs) else 0
    except Exception:
        return 0


def _find_last_crossover(ind, session_start=0):
    """
    Scan backward from the most recent bar to find the last EMA20/EMA50
    crossover event, never looking earlier than `session_start` (the
    first bar of today's session inside the current window) — see
    SAME-DAY (INTRADAY) BOUNDARY note above.

    Returns (direction, idx):
      direction = 'up'   -> EMA20 crossed above EMA50 (bullish setup started)
      direction = 'down' -> EMA20 crossed below EMA50 (bearish setup started)
      direction = None   -> no crossover found inside today's visible bars
      idx = positional index (0-based, into `ind`) of the bar where the
            crossover happened; None if direction is None.
    """
    e20 = ind['ema_20'].values
    e50 = ind['ema_50'].values
    n = len(ind)
    # i must stay > session_start so that i-1 (the "previous candle" in the
    # comparison) is also from today's session, not yesterday's last bar.
    lower_bound = max(1, session_start + 1)
    for i in range(n - 1, lower_bound - 1, -1):
        p20, p50, c20, c50 = e20[i - 1], e50[i - 1], e20[i], e50[i]
        if np.isnan(p20) or np.isnan(p50) or np.isnan(c20) or np.isnan(c50):
            continue
        if p20 <= p50 and c20 > c50:
            return 'up', i
        if p20 >= p50 and c20 < c50:
            return 'down', i
    return None, None


def _eligible_bar(e20_val, e50_val, close_val, want_bullish):
    """
    Checks Step 3 (EMA Difference >= 0.50%) and Step 4 (close confirmation)
    for a single completed candle, given precomputed EMA20/EMA50/close
    values for that candle.
    """
    if np.isnan(e20_val) or np.isnan(e50_val) or e50_val == 0:
        return False
    if want_bullish:
        diff_pct = ((e20_val - e50_val) / e50_val) * 100.0
        return diff_pct >= EMA_DIFF_THRESHOLD_PCT and close_val > e20_val and close_val > e50_val
    else:
        diff_pct = ((e50_val - e20_val) / e50_val) * 100.0
        return diff_pct >= EMA_DIFF_THRESHOLD_PCT and close_val < e20_val and close_val < e50_val


def _signal(df, ind, want_bullish):
    """
    Shared logic for both directions:
      1. Find the most recent EMA20/EMA50 crossover in the window.
      2. If its direction doesn't match what we're looking for, there is
         no active setup for this side (either no crossover yet, or the
         setup was cancelled by an opposite crossover) -> no signal.
      3. Check Step 3 + Step 4 on the current (last) candle.
      4. Make sure this is the FIRST candle since the crossover where
         Step 3 + Step 4 both held — i.e. don't re-fire every bar while
         price/EMA stay extended past the threshold.
    """
    if 'ema_20' not in ind.columns or 'ema_50' not in ind.columns:
        return False
    if len(df) < MIN_BARS_REQUIRED or len(ind) < 2:
        return False

    # Bound crossover search + eligibility scan to TODAY's session only —
    # see SAME-DAY (INTRADAY) BOUNDARY note in the module docstring. Without
    # this, a crossover from a previous session (still present in the
    # rolling multi-day window early in the day) gets treated as an
    # "active" setup for today, which is wrong for an intraday strategy
    # where nothing carries over between sessions.
    session_start = _session_start_idx(df)

    direction, cross_idx = _find_last_crossover(ind, session_start=session_start)
    wanted_dir = 'up' if want_bullish else 'down'
    if direction != wanted_dir:
        return False  # no active setup on this side right now (today)

    e20 = ind['ema_20']
    e50 = ind['ema_50']
    close = df['close']
    last_i = len(ind) - 1

    e20_now = float(e20.iloc[last_i])
    e50_now = float(e50.iloc[last_i])
    close_now = float(close.iloc[last_i])

    if not _eligible_bar(e20_now, e50_now, close_now, want_bullish):
        return False

    # Don't repeat-fire: confirm no earlier bar since the crossover already
    # satisfied Step 3 + Step 4 (that earlier bar would have been the real
    # signal candle).
    for i in range(cross_idx + 1, last_i):
        e20_i = float(e20.iloc[i])
        e50_i = float(e50.iloc[i])
        close_i = float(close.iloc[i])
        if _eligible_bar(e20_i, e50_i, close_i, want_bullish):
            return False

    return True


def confluence_buy(df, ind):
    """
    BUY signal — see module docstring for the full 5-step spec:
      Step1: EMA20 crossed above EMA50 (somewhere in the visible window)
      Step2: no opposite (bearish) crossover happened since then
      Step3: EMA Difference % = (EMA20-EMA50)/EMA50*100 >= 0.50 on this candle
      Step4: Close > EMA20 AND Close > EMA50 on this candle
      Step5: fires on the first candle where Step3+Step4 both hold
    """
    try:
        signal = _signal(df, ind, want_bullish=True)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            e20 = float(ind['ema_20'].iloc[-1])
            e50 = float(ind['ema_50'].iloc[-1])
            close_now = float(df['close'].iloc[-1])
            diff_pct = ((e20 - e50) / e50) * 100.0
            logger.info(
                f"EMA_MOMENTUM_BUY: {sym} close={close_now:.2f} "
                f"ema20={e20:.2f} ema50={e50:.2f} ema_diff_pct={diff_pct:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"EMA_MOMENTUM_BUY error: {e}")
        return False


def confluence_sell(df, ind):
    """
    SELL signal — exact mirror of confluence_buy:
      Step1: EMA20 crossed below EMA50
      Step2: no opposite (bullish) crossover happened since then
      Step3: EMA Difference % = (EMA50-EMA20)/EMA50*100 >= 0.50 on this candle
      Step4: Close < EMA20 AND Close < EMA50 on this candle
      Step5: fires on the first candle where Step3+Step4 both hold
    """
    try:
        signal = _signal(df, ind, want_bullish=False)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            e20 = float(ind['ema_20'].iloc[-1])
            e50 = float(ind['ema_50'].iloc[-1])
            close_now = float(df['close'].iloc[-1])
            diff_pct = ((e50 - e20) / e50) * 100.0
            logger.info(
                f"EMA_MOMENTUM_SELL: {sym} close={close_now:.2f} "
                f"ema20={e20:.2f} ema50={e50:.2f} ema_diff_pct={diff_pct:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"EMA_MOMENTUM_SELL error: {e}")
        return False


# --------------- REGISTRY ---------------
all_strategies = {
    'EMA_MOMENTUM_BUY': confluence_buy,
    'EMA_MOMENTUM_SELL': confluence_sell,
}

# --------------- METADATA ---------------
# Direction now matches the function names directly — no inversion.
# category='trend': this strategy enters WITH an already-established
# (and now momentum-confirmed) trend rather than at a fresh breakout, so
# it stays subject to the engine's normal overextension vetoes in
# _check_entry_quality (skip_extension_checks only exempts
# 'breakout'/'momentum' categories).
strategy_meta = {
    'EMA_MOMENTUM_BUY': {'direction': 'BUY', 'category': 'trend'},
    'EMA_MOMENTUM_SELL': {'direction': 'SELL', 'category': 'trend'},
}