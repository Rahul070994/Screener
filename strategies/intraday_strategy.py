# confluence_strategy.py — EMA20/EMA50 Momentum Confirmation (v9): BUY on EMA20 crossing above EMA50 then Open/Close > EMA20 & EMA50 within MAX_SIGNAL_DELAY_MINUTES, AND Close breaks above the highest high made so far in today's session, AND Close clears the Camarilla R3 (breakout) level from the previous session with a minimum buffer AND minimum room before R4; SELL is the mirror (Open/Close < both EMAs, Close breaks below today's lowest low, Close clears S3 with buffer + room before S4).

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

MIN_BARS_REQUIRED = 100      # EMA50 warm-up buffer
EMA_DIFF_THRESHOLD_PCT = 0.50  # required |EMA20-EMA50| / EMA50 * 100 before entry
MAX_SIGNAL_DELAY_MINUTES = 60  # crossover must confirm within this window or it's stale
TIMEFRAME = "3minute"  # this strategy's own candle interval, read by ultimate_scanner.py

# --- Camarilla pivot confirmation (Step 6) ---
# R3/S3 are Camarilla's breakout-trigger levels; R4/S4 are the extended
# exhaustion/target levels. A close that has *just* poked past R3/S3 is
# a weak, easily-reversed breakout (price could snap back any moment) —
# so we require a minimum buffer beyond R3/S3, AND minimum room before
# hitting R4/S4 (so the trade isn't entering right under a ceiling).
CAMARILLA_MIN_BUFFER_PCT = 0.15  # close must clear R3/S3 by at least this % to count as a real break, not a poke
CAMARILLA_MIN_ROOM_PCT = 0.30    # close must still be at least this % away from R4/S4 (exhaustion zone)


def _session_start_idx(df):
    """Index of the first candle of today's session inside the window (0 if unknown)."""
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


def _find_last_crossover(ind, session_start=0):
    """Scan backward for the last EMA20/EMA50 crossover within today's session; returns (direction, idx)."""
    e20 = ind['ema_20'].values
    e50 = ind['ema_50'].values
    n = len(ind)
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


def _prior_session_extreme(df, session_start, i, want_bullish):
    """
    Highest 'high' (BUY) / lowest 'low' (SELL) among today's candles strictly
    before index i (i.e. df.iloc[session_start:i] — session_start to i-1
    inclusive, i itself excluded). Used as the breakout reference level that
    the *current* candle's close must clear before an entry is allowed.
    Returns None if there are no prior candles today to measure (i is the
    first candle of the session) or the high/low columns are missing.
    """
    lo = max(session_start, 0)
    if i <= lo or 'high' not in df.columns or 'low' not in df.columns:
        return None
    window = df.iloc[lo:i]
    if len(window) == 0:
        return None
    try:
        return float(window['high'].max()) if want_bullish else float(window['low'].min())
    except Exception:
        return None


def _previous_session_ohlc(df, session_start):
    """
    High/Low/Close of the trading day immediately before today (today =
    the day session_start belongs to). df already spans several calendar
    days (the scanner fetches a multi-day lookback window for EMA50
    warm-up), so the previous session's candles are simply the rows
    right before session_start, filtered down to that single date.
    Returns None if there's no prior day in the window.
    """
    if 'date' not in df.columns or session_start <= 0:
        return None
    try:
        dates = pd.to_datetime(df['date'])
        prev_day_date = dates.iloc[session_start - 1].date()
        mask = dates.dt.date.values == prev_day_date
        prev_df = df.loc[mask]
        if len(prev_df) == 0 or 'high' not in prev_df.columns or 'low' not in prev_df.columns:
            return None
        return {
            'high': float(prev_df['high'].max()),
            'low': float(prev_df['low'].min()),
            'close': float(prev_df['close'].iloc[-1]),
        }
    except Exception:
        return None


def _camarilla_levels(prev_ohlc):
    """Classic Camarilla R1-R4/S1-S4 from the previous session's H/L/C. Only R3/R4 (S3/S4) are used as entry gates; R1/R2/S1/S2 sit inside the day's 'noise zone' and aren't meaningful breakout levels on their own."""
    if prev_ohlc is None:
        return None
    prev_high, prev_low, prev_close = prev_ohlc['high'], prev_ohlc['low'], prev_ohlc['close']
    rng = prev_high - prev_low
    if rng <= 0 or np.isnan(rng):
        return None
    return {
        'R4': prev_close + rng * 1.1 / 2,
        'R3': prev_close + rng * 1.1 / 4,
        'R2': prev_close + rng * 1.1 / 6,
        'R1': prev_close + rng * 1.1 / 12,
        'S1': prev_close - rng * 1.1 / 12,
        'S2': prev_close - rng * 1.1 / 6,
        'S3': prev_close - rng * 1.1 / 4,
        'S4': prev_close - rng * 1.1 / 2,
    }


def _camarilla_confirmed(close_val, levels, want_bullish):
    """
    Step 6: Close must clear the Camarilla breakout level (R3 for BUY,
    S3 for SELL) by at least CAMARILLA_MIN_BUFFER_PCT — a bare poke past
    the level isn't trusted, since a small pullback could immediately
    put price back on the wrong side. Close must ALSO still be at least
    CAMARILLA_MIN_ROOM_PCT away from the next exhaustion level (R4/S4),
    so we're not buying right under a ceiling / selling right above a floor.
    """
    if levels is None or np.isnan(close_val):
        return False
    if want_bullish:
        r3, r4 = levels['R3'], levels['R4']
        if r3 <= 0 or np.isnan(r3) or np.isnan(r4):
            return False
        if close_val <= r3:
            return False
        buffer_pct = (close_val - r3) / r3 * 100.0
        if buffer_pct < CAMARILLA_MIN_BUFFER_PCT:
            return False
        if r4 > r3:
            room_pct = (r4 - close_val) / close_val * 100.0
            if room_pct < CAMARILLA_MIN_ROOM_PCT:
                return False
        return True
    else:
        s3, s4 = levels['S3'], levels['S4']
        if s3 <= 0 or np.isnan(s3) or np.isnan(s4):
            return False
        if close_val >= s3:
            return False
        buffer_pct = (s3 - close_val) / s3 * 100.0
        if buffer_pct < CAMARILLA_MIN_BUFFER_PCT:
            return False
        if s4 < s3:
            room_pct = (close_val - s4) / close_val * 100.0
            if room_pct < CAMARILLA_MIN_ROOM_PCT:
                return False
        return True


def _eligible_bar(e20_val, e50_val, close_val, open_val, want_bullish, e20_at_cross, breakout_ref, camarilla_levels):
    """Step 3 (EMA20 has moved >= threshold% from its value AT the crossover bar) + Step 4 (Open AND Close beyond both EMAs) + Step 5 (Close breaks the prior session high/low) + Step 6 (Close clears Camarilla R3/S3 with buffer + room before R4/S4)."""
    if np.isnan(e20_val) or np.isnan(e50_val) or np.isnan(open_val) or np.isnan(e20_at_cross) or e20_at_cross == 0:
        return False
    if breakout_ref is None or np.isnan(breakout_ref):
        return False
    if not _camarilla_confirmed(close_val, camarilla_levels, want_bullish):
        return False
    if want_bullish:
        diff_pct = ((e20_val - e20_at_cross) / e20_at_cross) * 100.0
        return (
            diff_pct >= EMA_DIFF_THRESHOLD_PCT
            and open_val > e20_val
            and close_val > e20_val
            and open_val > e50_val
            and close_val > e50_val
            and close_val > breakout_ref  # Step 5: close must break above the highest high made so far today
        )
    else:
        diff_pct = ((e20_at_cross - e20_val) / e20_at_cross) * 100.0
        return (
            diff_pct >= EMA_DIFF_THRESHOLD_PCT
            and open_val < e20_val
            and close_val < e20_val
            and open_val < e50_val
            and close_val < e50_val
            and close_val < breakout_ref  # Step 5: close must break below the lowest low made so far today
        )


def _minutes_between(df, idx_from, idx_to):
    """Minutes between two bars' timestamps; None if 'date' is missing/unparsable."""
    if 'date' not in df.columns:
        return None
    try:
        dates = pd.to_datetime(df['date'])
        delta = dates.iloc[idx_to] - dates.iloc[idx_from]
        return delta.total_seconds() / 60.0
    except Exception:
        return None


def _signal(df, ind, want_bullish):
    """Find the latest same-side crossover, check it's not stale, then fire only on the first eligible bar since it."""
    if 'ema_20' not in ind.columns or 'ema_50' not in ind.columns:
        return False
    if len(df) < MIN_BARS_REQUIRED or len(ind) < 2:
        return False

    session_start = _session_start_idx(df)  # bound scan to today's session only

    direction, cross_idx = _find_last_crossover(ind, session_start=session_start)
    wanted_dir = 'up' if want_bullish else 'down'
    if direction != wanted_dir:
        return False  # no active setup on this side right now (today)

    e20 = ind['ema_20']
    e50 = ind['ema_50']
    close = df['close']
    open_ = df['open']
    last_i = len(ind) - 1
    e20_at_cross = float(e20.iloc[cross_idx])  # fixed reference point: EMA20's value at the crossover bar

    age_minutes = _minutes_between(df, cross_idx, last_i)  # staleness guard
    if age_minutes is not None and age_minutes > MAX_SIGNAL_DELAY_MINUTES:
        return False

    # Breakout reference is now FROZEN at the crossover bar (inclusive) and
    # never updated again for this setup.
    breakout_ref = _prior_session_extreme(df, session_start, cross_idx + 1, want_bullish)

    # Camarilla levels come from the previous session's H/L/C, so they're
    # fixed for the whole of today — computed once per _signal call.
    camarilla_levels = _camarilla_levels(_previous_session_ohlc(df, session_start))

    e20_now = float(e20.iloc[last_i])
    e50_now = float(e50.iloc[last_i])
    close_now = float(close.iloc[last_i])
    open_now = float(open_.iloc[last_i])

    if not _eligible_bar(e20_now, e50_now, close_now, open_now, want_bullish, e20_at_cross, breakout_ref, camarilla_levels):
        return False

    for i in range(cross_idx + 1, last_i):  # don't repeat-fire past the first eligible bar
        e20_i = float(e20.iloc[i])
        e50_i = float(e50.iloc[i])
        close_i = float(close.iloc[i])
        open_i = float(open_.iloc[i])
        if _eligible_bar(e20_i, e50_i, close_i, open_i, want_bullish, e20_at_cross, breakout_ref, camarilla_levels):
            return False

    return True


def confluence_buy(df, ind):
    """BUY: EMA20 crosses above EMA50, then EMA20 moves >= threshold% above its crossover-bar value, with Open>EMA20, Close>EMA20, Open>EMA50, Close>EMA50, Close breaks above the highest high made so far in today's session, AND Close clears the previous session's Camarilla R3 by >= CAMARILLA_MIN_BUFFER_PCT while staying >= CAMARILLA_MIN_ROOM_PCT away from R4 — on the first qualifying candle within the delay window."""
    try:
        signal = _signal(df, ind, want_bullish=True)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            e20 = float(ind['ema_20'].iloc[-1])
            e50 = float(ind['ema_50'].iloc[-1])
            close_now = float(df['close'].iloc[-1])
            open_now = float(df['open'].iloc[-1])
            session_start = _session_start_idx(df)
            _, cross_idx = _find_last_crossover(ind, session_start=session_start)
            e20_at_cross = float(ind['ema_20'].iloc[cross_idx])
            diff_pct = ((e20 - e20_at_cross) / e20_at_cross) * 100.0
            logger.info(
                f"EMA_MOMENTUM_BUY: {sym} open={open_now:.2f} close={close_now:.2f} "
                f"ema20={e20:.2f} ema50={e50:.2f} ema20_at_cross={e20_at_cross:.2f} ema20_move_pct={diff_pct:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"EMA_MOMENTUM_BUY error: {e}")
        return False


def confluence_sell(df, ind):
    """SELL: exact mirror of confluence_buy — EMA20 crosses below EMA50, then EMA20 moves >= threshold% below its crossover-bar value, with Open<EMA20, Close<EMA20, Open<EMA50, Close<EMA50, Close breaks below the lowest low made so far in today's session, AND Close clears the previous session's Camarilla S3 by >= CAMARILLA_MIN_BUFFER_PCT while staying >= CAMARILLA_MIN_ROOM_PCT away from S4."""
    try:
        signal = _signal(df, ind, want_bullish=False)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            e20 = float(ind['ema_20'].iloc[-1])
            e50 = float(ind['ema_50'].iloc[-1])
            close_now = float(df['close'].iloc[-1])
            open_now = float(df['open'].iloc[-1])
            session_start = _session_start_idx(df)
            _, cross_idx = _find_last_crossover(ind, session_start=session_start)
            e20_at_cross = float(ind['ema_20'].iloc[cross_idx])
            diff_pct = ((e20_at_cross - e20) / e20_at_cross) * 100.0
            logger.info(
                f"EMA_MOMENTUM_SELL: {sym} open={open_now:.2f} close={close_now:.2f} "
                f"ema20={e20:.2f} ema50={e50:.2f} ema20_at_cross={e20_at_cross:.2f} ema20_move_pct={diff_pct:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"EMA_MOMENTUM_SELL error: {e}")
        return False


# Early-exit check for the live Signal Log / position monitor — called by
# the scanner (via strategy_exits, keyed by the strategy name that opened
# the position) once per pass on an OPEN position, ahead of target/SL.
# Returning True closes the trade immediately at market, regardless of
# where price sits relative to target/SL. All strategy-specific knowledge
# of what counts as "reversed" (EMA20/50 flip, in this strategy's case)
# stays here — the scanner just calls whatever function this strategy
# registers and acts on True/False, so it never needs to know EMA20/EMA50
# even exist.
def _ema_momentum_reversal_exit(df, ind, pos):
    try:
        if 'ema_20' not in ind.columns or 'ema_50' not in ind.columns or len(ind) < 1:
            return False
        e20 = float(ind['ema_20'].iloc[-1])
        e50 = float(ind['ema_50'].iloc[-1])
        if np.isnan(e20) or np.isnan(e50):
            return False
        side = pos.get('side')
        if side == 'BUY':
            return e20 < e50   # was above EMA50 at entry, now crossed back below
        if side == 'SELL':
            return e20 > e50   # was below EMA50 at entry, now crossed back above
        return False
    except Exception as e:
        logger.debug(f"EMA_MOMENTUM reversal-exit check error: {e}")
        return False


# Optional diagnostics for the live Signal Log UI — shows the strategy's own decision variables per bar, doesn't affect entry logic.
def _ema_momentum_diagnostics(df, ind):
    try:
        if 'ema_20' not in ind.columns or 'ema_50' not in ind.columns or len(ind) < 2:
            return {}
        session_start = _session_start_idx(df)
        direction, cross_idx = _find_last_crossover(ind, session_start=session_start)
        e20 = float(ind['ema_20'].iloc[-1])
        e50 = float(ind['ema_50'].iloc[-1])
        close = float(df['close'].iloc[-1])
        open_now = float(df['open'].iloc[-1])
        if np.isnan(e20) or np.isnan(e50) or e50 == 0:
            return {}
        if cross_idx is not None:
            e20_at_cross = float(ind['ema_20'].iloc[cross_idx])
        else:
            e20_at_cross = float('nan')
        if direction == 'up' and not np.isnan(e20_at_cross) and e20_at_cross != 0:
            diff_pct = ((e20 - e20_at_cross) / e20_at_cross) * 100.0
        elif direction == 'down' and not np.isnan(e20_at_cross) and e20_at_cross != 0:
            diff_pct = ((e20_at_cross - e20) / e20_at_cross) * 100.0
        else:
            diff_pct = 0.0
        setup = 'Bullish (EMA20>EMA50)' if direction == 'up' else 'Bearish (EMA20<EMA50)' if direction == 'down' else 'No crossover yet'
        breakout_ref = _prior_session_extreme(df, session_start, cross_idx + 1, direction == 'up') if cross_idx is not None else None
        out = {
            'EMA20': round(e20, 2),
            'EMA50': round(e50, 2),
            'Open': round(open_now, 2),
            'Close': round(close, 2),
            'Diff%': round(diff_pct, 2),
            'Threshold%': EMA_DIFF_THRESHOLD_PCT,
            'Setup': setup,
        }
        if cross_idx is not None:
            try:
                out['Crossed@'] = str(df['date'].iloc[cross_idx])[-8:-3] if 'date' in df.columns else cross_idx
            except Exception:
                pass
            age_minutes = _minutes_between(df, cross_idx, len(df) - 1)
            if age_minutes is not None:
                out['SetupAge(min)'] = round(age_minutes, 1)
                out['MaxAge(min)'] = MAX_SIGNAL_DELAY_MINUTES
                out['Stale'] = age_minutes > MAX_SIGNAL_DELAY_MINUTES
        if breakout_ref is not None and not np.isnan(breakout_ref):
            out['BreakoutRef'] = round(breakout_ref, 2)
            out['BreakoutCleared'] = (close > breakout_ref) if direction == 'up' else (close < breakout_ref)

        camarilla_levels = _camarilla_levels(_previous_session_ohlc(df, session_start))
        if camarilla_levels is not None:
            want_bullish = (direction == 'up')
            trigger_key = 'R3' if want_bullish else 'S3'
            exhaustion_key = 'R4' if want_bullish else 'S4'
            trigger_val = camarilla_levels[trigger_key]
            exhaustion_val = camarilla_levels[exhaustion_key]
            out['Camarilla_' + trigger_key] = round(trigger_val, 2)
            out['Camarilla_' + exhaustion_key] = round(exhaustion_val, 2)
            if trigger_val:
                buffer_pct = ((close - trigger_val) / trigger_val * 100.0) if want_bullish else ((trigger_val - close) / trigger_val * 100.0)
                out['CamarillaBuffer%'] = round(buffer_pct, 2)
            if close:
                room_pct = ((exhaustion_val - close) / close * 100.0) if want_bullish else ((close - exhaustion_val) / close * 100.0)
                out['CamarillaRoom%'] = round(room_pct, 2)
            out['CamarillaConfirmed'] = _camarilla_confirmed(close, camarilla_levels, want_bullish)
        return out
    except Exception as e:
        logger.debug(f"EMA_MOMENTUM diagnostics error: {e}")
        return {}


strategy_diagnostics = {
    'EMA_MOMENTUM_BUY': _ema_momentum_diagnostics,
    'EMA_MOMENTUM_SELL': _ema_momentum_diagnostics,
}

# Optional per-strategy early-exit functions {strategy_name: fn(df, ind, pos) -> bool}.
# A strategy only needs an entry here if it wants to close a position early
# (ahead of target/SL) when its own setup invalidates. Strategies that don't
# define one simply aren't in this dict, and the scanner skips the check.
strategy_exits = {
    'EMA_MOMENTUM_BUY': _ema_momentum_reversal_exit,
    'EMA_MOMENTUM_SELL': _ema_momentum_reversal_exit,
}

all_strategies = {
    'EMA_MOMENTUM_BUY': confluence_buy,
    'EMA_MOMENTUM_SELL': confluence_sell,
}

# category='momentum' skips the generic extension vetoes (correct here, since Step 3 IS the extension); skip_quality_checks=True bypasses volume/candle vetoes so only the EMA logic above gates entry.
strategy_meta = {
    'EMA_MOMENTUM_BUY': {'direction': 'BUY', 'category': 'momentum', 'skip_quality_checks': True},
    'EMA_MOMENTUM_SELL': {'direction': 'SELL', 'category': 'momentum', 'skip_quality_checks': True},
}