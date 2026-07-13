# ema_pullback_strategy_v3.py — ATR‑adaptive EMA pullback
# ================================================================
# Adds:
#   - ATR(14) for dynamic stop (1.5×ATR) and target (3×ATR)
#   - RSI filter (>50 for buy, <50 for sell)
#   - 200 EMA trend filter
#   - Increased volume threshold (2× average)
#   - Retracement min 50%
# ================================================================

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

_EMA_TOUCH_PCT = 0.001
_MIN_EMA_GAP_PCT = 0.1
_SLOPE_LOOKBACK = 1
_MIN_RETRACEMENT = 0.50          # 50% minimum pullback
_VOLUME_MULTIPLIER = 2.0         # volume > 2× average
_ATR_MULTIPLIER_STOP = 1.5
_ATR_MULTIPLIER_TARGET = 3.0

def _fv(row, key, fallback=np.nan):
    try:
        v = float(row[key])
        return fallback if np.isnan(v) else v
    except Exception:
        return fallback

def _pullback_depth(df, side):
    recent_high = df['high'].iloc[-10:].max()
    recent_low = df['low'].iloc[-10:].min()
    current_close = df['close'].iloc[-1]
    if recent_high == recent_low:
        return 0.0
    if side == 'buy':
        return (recent_high - current_close) / (recent_high - recent_low)
    else:
        return (current_close - recent_low) / (recent_high - recent_low)

def ema_pullback_buy(df, ind):
    try:
        if len(df) < 30 or len(ind) < 30:
            return False

        i_curr = ind.iloc[-1]
        i_prev = ind.iloc[-2]

        ema20_now = _fv(i_curr, 'ema_20')
        ema50_now = _fv(i_curr, 'ema_50')
        ema200_now = _fv(i_curr, 'ema_200') if 'ema_200' in i_curr.index else _fv(i_curr, 'ema_200', np.nan)
        ema20_prev = _fv(i_prev, 'ema_20')
        adx = _fv(i_curr, 'adx')
        rsi = _fv(i_curr, 'rsi') if 'rsi' in i_curr.index else np.nan
        atr = _fv(i_curr, 'atr') if 'atr' in i_curr.index else np.nan

        if any(np.isnan(x) for x in [ema20_now, ema50_now, ema20_prev, atr]):
            return False

        close = float(df['close'].iloc[-1])
        low_now = float(df['low'].iloc[-1])
        prev_high = float(df['high'].iloc[-2])
        volume_now = float(df['volume'].iloc[-1])
        avg_volume = float(df['volume'].iloc[-20:].mean()) if len(df) >= 20 else volume_now

        # ---- Trend ----
        uptrend = close > ema20_now > ema50_now
        if not np.isnan(ema200_now):
            uptrend = uptrend and close > ema200_now
        ema20_sloping_up = ema20_now > ema20_prev
        near_ema20 = low_now <= ema20_now * (1.0 + _EMA_TOUCH_PCT)
        bullish_trigger = close > prev_high

        # ---- Strength ----
        gap_pct = ((ema20_now - ema50_now) / close * 100) if close > 0 else 0.0
        trend_strength_ok = (not np.isnan(adx) and adx > 20) or gap_pct > _MIN_EMA_GAP_PCT

        # ---- Volume ----
        volume_ok = volume_now > avg_volume * _VOLUME_MULTIPLIER

        # ---- Retracement ----
        retrace_pct = _pullback_depth(df, 'buy')
        retrace_ok = retrace_pct >= _MIN_RETRACEMENT

        # ---- RSI ----
        rsi_ok = True
        if not np.isnan(rsi):
            rsi_ok = rsi > 50

        # ---- IV ----
        iv_ok = True
        if 'iv_percentile' in i_curr.index:
            iv = _fv(i_curr, 'iv_percentile')
            if not np.isnan(iv):
                iv_ok = iv < 60

        result = (uptrend and ema20_sloping_up and near_ema20 and
                  bullish_trigger and trend_strength_ok and volume_ok and
                  retrace_ok and rsi_ok and iv_ok)

        if result:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            logger.info(
                f"EMA_PB_BUY: {sym} close={close:.2f} EMA20={ema20_now:.2f} "
                f"gap%={gap_pct:.2f} adx={adx:.1f} retrace={retrace_pct:.1%} vol={volume_now/avg_volume:.1f}x atr={atr:.2f}"
            )
        return result

    except Exception as e:
        logger.error(f"EMA_PULLBACK_BUY error: {e}")
        return False

def ema_pullback_sell(df, ind):
    try:
        if len(df) < 30 or len(ind) < 30:
            return False

        i_curr = ind.iloc[-1]
        i_prev = ind.iloc[-2]

        ema20_now = _fv(i_curr, 'ema_20')
        ema50_now = _fv(i_curr, 'ema_50')
        ema200_now = _fv(i_curr, 'ema_200') if 'ema_200' in i_curr.index else np.nan
        ema20_prev = _fv(i_prev, 'ema_20')
        adx = _fv(i_curr, 'adx')
        rsi = _fv(i_curr, 'rsi') if 'rsi' in i_curr.index else np.nan
        atr = _fv(i_curr, 'atr') if 'atr' in i_curr.index else np.nan

        if any(np.isnan(x) for x in [ema20_now, ema50_now, ema20_prev, atr]):
            return False

        close = float(df['close'].iloc[-1])
        high_now = float(df['high'].iloc[-1])
        prev_low = float(df['low'].iloc[-2])
        volume_now = float(df['volume'].iloc[-1])
        avg_volume = float(df['volume'].iloc[-20:].mean()) if len(df) >= 20 else volume_now

        downtrend = close < ema20_now < ema50_now
        if not np.isnan(ema200_now):
            downtrend = downtrend and close < ema200_now
        ema20_sloping_dn = ema20_now < ema20_prev
        near_ema20 = high_now >= ema20_now * (1.0 - _EMA_TOUCH_PCT)
        bearish_trigger = close < prev_low

        gap_pct = ((ema50_now - ema20_now) / close * 100) if close > 0 else 0.0
        trend_strength_ok = (not np.isnan(adx) and adx > 20) or gap_pct > _MIN_EMA_GAP_PCT

        volume_ok = volume_now > avg_volume * _VOLUME_MULTIPLIER

        retrace_pct = _pullback_depth(df, 'sell')
        retrace_ok = retrace_pct >= _MIN_RETRACEMENT

        rsi_ok = True
        if not np.isnan(rsi):
            rsi_ok = rsi < 50

        iv_ok = True
        if 'iv_percentile' in i_curr.index:
            iv = _fv(i_curr, 'iv_percentile')
            if not np.isnan(iv):
                iv_ok = iv < 60

        result = (downtrend and ema20_sloping_dn and near_ema20 and
                  bearish_trigger and trend_strength_ok and volume_ok and
                  retrace_ok and rsi_ok and iv_ok)

        if result:
            sym = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
            logger.info(
                f"EMA_PB_SELL: {sym} close={close:.2f} EMA20={ema20_now:.2f} "
                f"gap%={gap_pct:.2f} adx={adx:.1f} retrace={retrace_pct:.1%} vol={volume_now/avg_volume:.1f}x atr={atr:.2f}"
            )
        return result

    except Exception as e:
        logger.error(f"EMA_PULLBACK_SELL error: {e}")
        return False


all_strategies = {
    'EMA_PULLBACK_BUY':  ema_pullback_buy,
    'EMA_PULLBACK_SELL': ema_pullback_sell,
}

MIN_BARS_REQUIRED = 60

strategy_meta = {
    'EMA_PULLBACK_BUY':  {'direction': 'BUY',  'category': 'pullback'},
    'EMA_PULLBACK_SELL': {'direction': 'SELL', 'category': 'pullback'},
}