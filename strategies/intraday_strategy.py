# confluence_strategy.py — EMA20/EMA50 Momentum Confirmation (v7): BUY on EMA20 crossing above EMA50 then Open/Close > EMA20 & EMA50 within MAX_SIGNAL_DELAY_MINUTES; SELL is the mirror with Open/Close < both EMAs.

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

MIN_BARS_REQUIRED = 100      # EMA50 warm-up buffer
EMA_DIFF_THRESHOLD_PCT = 0.50  # required |EMA20-EMA50| / EMA50 * 100 before entry
MAX_SIGNAL_DELAY_MINUTES = 30  # crossover must confirm within this window or it's stale
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


def _eligible_bar(e20_val, e50_val, close_val, open_val, want_bullish):
    """Step 3 (EMA diff >= threshold) + Step 4 (Open AND Close beyond both EMAs) for one candle."""
    if np.isnan(e20_val) or np.isnan(e50_val) or np.isnan(open_val) or e50_val == 0:
        return False
    if want_bullish:
        diff_pct = ((e20_val - e50_val) / e50_val) * 100.0
        return (
            diff_pct >= EMA_DIFF_THRESHOLD_PCT
            and open_val > e20_val
            and close_val > e20_val
            and open_val > e50_val
            and close_val > e50_val
        )
    else:
        diff_pct = ((e50_val - e20_val) / e50_val) * 100.0
        return (
            diff_pct >= EMA_DIFF_THRESHOLD_PCT
            and open_val < e20_val
            and close_val < e20_val
            and open_val < e50_val
            and close_val < e50_val
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

    age_minutes = _minutes_between(df, cross_idx, last_i)  # staleness guard
    if age_minutes is not None and age_minutes > MAX_SIGNAL_DELAY_MINUTES:
        return False

    e20_now = float(e20.iloc[last_i])
    e50_now = float(e50.iloc[last_i])
    close_now = float(close.iloc[last_i])
    open_now = float(open_.iloc[last_i])

    if not _eligible_bar(e20_now, e50_now, close_now, open_now, want_bullish):
        return False

    for i in range(cross_idx + 1, last_i):  # don't repeat-fire past the first eligible bar
        e20_i = float(e20.iloc[i])
        e50_i = float(e50.iloc[i])
        close_i = float(close.iloc[i])
        open_i = float(open_.iloc[i])
        if _eligible_bar(e20_i, e50_i, close_i, open_i, want_bullish):
            return False

    return True


def confluence_buy(df, ind):
    """BUY: EMA20 crosses above EMA50, then Open>EMA20, Close>EMA20, Open>EMA50, Close>EMA50 on the first qualifying candle within the delay window."""
    try:
        signal = _signal(df, ind, want_bullish=True)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            e20 = float(ind['ema_20'].iloc[-1])
            e50 = float(ind['ema_50'].iloc[-1])
            close_now = float(df['close'].iloc[-1])
            open_now = float(df['open'].iloc[-1])
            diff_pct = ((e20 - e50) / e50) * 100.0
            logger.info(
                f"EMA_MOMENTUM_BUY: {sym} open={open_now:.2f} close={close_now:.2f} "
                f"ema20={e20:.2f} ema50={e50:.2f} ema_diff_pct={diff_pct:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"EMA_MOMENTUM_BUY error: {e}")
        return False


def confluence_sell(df, ind):
    """SELL: exact mirror of confluence_buy — EMA20 crosses below EMA50, then Open<EMA20, Close<EMA20, Open<EMA50, Close<EMA50."""
    try:
        signal = _signal(df, ind, want_bullish=False)
        if signal:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            e20 = float(ind['ema_20'].iloc[-1])
            e50 = float(ind['ema_50'].iloc[-1])
            close_now = float(df['close'].iloc[-1])
            open_now = float(df['open'].iloc[-1])
            diff_pct = ((e50 - e20) / e50) * 100.0
            logger.info(
                f"EMA_MOMENTUM_SELL: {sym} open={open_now:.2f} close={close_now:.2f} "
                f"ema20={e20:.2f} ema50={e50:.2f} ema_diff_pct={diff_pct:.2f}"
            )
        return bool(signal)
    except Exception as e:
        logger.error(f"EMA_MOMENTUM_SELL error: {e}")
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
        if direction == 'up':
            diff_pct = ((e20 - e50) / e50) * 100.0
        elif direction == 'down':
            diff_pct = ((e50 - e20) / e50) * 100.0
        else:
            diff_pct = 0.0
        setup = 'Bullish (EMA20>EMA50)' if direction == 'up' else 'Bearish (EMA20<EMA50)' if direction == 'down' else 'No crossover yet'
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
        return out
    except Exception as e:
        logger.debug(f"EMA_MOMENTUM diagnostics error: {e}")
        return {}


strategy_diagnostics = {
    'EMA_MOMENTUM_BUY': _ema_momentum_diagnostics,
    'EMA_MOMENTUM_SELL': _ema_momentum_diagnostics,
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