# confluence_strategy.py — EMA20/EMA50 Momentum Confirmation (v8): BUY on EMA20 crossing above EMA50 then Open/Close > EMA20 & EMA50 within MAX_SIGNAL_DELAY_MINUTES, AND Close breaks above the highest high made so far in today's session; SELL is the mirror (Open/Close < both EMAs, Close breaks below today's lowest low).

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

MIN_BARS_REQUIRED = 100      # EMA50 warm-up buffer
EMA_DIFF_THRESHOLD_PCT = 0.50  # required |EMA20-EMA50| / EMA50 * 100 before entry
MAX_SIGNAL_DELAY_MINUTES = 60  # crossover must confirm within this window or it's stale
TIMEFRAME = "3minute"  # this strategy's own candle interval, read by ultimate_scanner.py


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


def _eligible_bar(e20_val, e50_val, close_val, open_val, want_bullish, e20_at_cross, breakout_ref):
    """Step 3 (EMA20 has moved >= threshold% from its value AT the crossover bar) + Step 4 (Open AND Close beyond both EMAs) + Step 5 (Close breaks the prior session high/low)."""
    if np.isnan(e20_val) or np.isnan(e50_val) or np.isnan(open_val) or np.isnan(e20_at_cross) or e20_at_cross == 0:
        return False
    if breakout_ref is None or np.isnan(breakout_ref):
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

    e20_now = float(e20.iloc[last_i])
    e50_now = float(e50.iloc[last_i])
    close_now = float(close.iloc[last_i])
    open_now = float(open_.iloc[last_i])

    if not _eligible_bar(e20_now, e50_now, close_now, open_now, want_bullish, e20_at_cross, breakout_ref):
        return False

    for i in range(cross_idx + 1, last_i):  # don't repeat-fire past the first eligible bar
        e20_i = float(e20.iloc[i])
        e50_i = float(e50.iloc[i])
        close_i = float(close.iloc[i])
        open_i = float(open_.iloc[i])
        if _eligible_bar(e20_i, e50_i, close_i, open_i, want_bullish, e20_at_cross, breakout_ref):
            return False

    return True


def confluence_buy(df, ind):
    """BUY: EMA20 crosses above EMA50, then EMA20 moves >= threshold% above its crossover-bar value, with Open>EMA20, Close>EMA20, Open>EMA50, Close>EMA50, AND Close breaks above the highest high made so far in today's session — on the first qualifying candle within the delay window."""
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
    """SELL: exact mirror of confluence_buy — EMA20 crosses below EMA50, then EMA20 moves >= threshold% below its crossover-bar value, with Open<EMA20, Close<EMA20, Open<EMA50, Close<EMA50, AND Close breaks below the lowest low made so far in today's session."""
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