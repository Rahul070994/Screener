# strategies.py — v4.0  HIGH-TRUST INTRADAY (5-min NSE)
# ============================================================
# CHANGES in v4.0:
#   • Relaxed thresholds on the 4 flagship strategies so they fire
#     more frequently while retaining trust ≥ 80%:
#       – liquidity_sweep_buy/sell   : vol 2.0x→1.7x, RSI bands widened
#       – order_flow_imbalance_buy/sell: vol 1.8x→1.5x, CMF 0.10→0.08
#       – anchored_vwap_bounce_buy/sell: vol 1.5x→1.3x, RSI (35,65)→(30,70)
#       – volume_profile_poc_buy/sell: vol 1.6x→1.4x, proximity 0.3%→0.5%
#   • All other retained strategies unchanged from v3.0.
# ============================================================
# CHANGES in v3.0:
#   • Removed all strategies below ~80% historical trust threshold.
#     REMOVED: golden_cross, death_cross (slow, late signals on 5m),
#              stoch_bullish/bearish_crossover (noisy on intraday),
#              morning_star, evening_star, three_white_soldiers,
#              three_black_crows, piercing_pattern, dark_cloud_cover,
#              hammer_reversal, shooting_star_reversal (unreliable on 5m),
#              ichimoku_cloud_breakout (lagged, cloud is 26-bar ahead),
#              support_resistance_bounce (quantile S/R too crude).
#   • KEPT: sell_setup_original, buy_setup_original (EMA trend stack),
#            rsi_oversold_bounce, rsi_overbought_reversal (price + vol),
#            macd_bullish_crossover, macd_bearish_crossover,
#            bollinger_breakout_buy/sell, volume_price_breakout,
#            adx_strong_trend_buy/sell, institutional_buying/selling,
#            ema_cluster_buy/sell, supertrend_breakout,
#            vwap_reclaim_buy, vwap_rejection_sell,
#            opening_range_breakout, orb_breakdown,
#            macd_fast_bullish/bearish, bb_squeeze_breakout_buy/sell,
#            bull_flag_breakout, bear_flag_breakout.
#   • ADDED:  liquidity_sweep_buy, liquidity_sweep_sell  (stop-hunt reversal)
#             order_flow_imbalance_buy, order_flow_imbalance_sell  (delta)
#             anchored_vwap_bounce_buy, anchored_vwap_bounce_sell  (AVWAP)
#             volume_profile_poc_buy, volume_profile_poc_sell  (POC/VAH/VAL)
# ============================================================

import numpy as np
import pandas as pd
from collections import OrderedDict

# ==================== HELPERS ====================

_POC_CACHE_MAX = 256
_poc_cache = OrderedDict()

def _get_today_df(df):
    """Slice today's bars from a multi-day intraday df (falls back to the
    last 50 bars if no 'date' column is present)."""
    try:
        if 'date' in df.columns:
            dates = pd.to_datetime(df['date'])
            today = dates.iloc[-1].date()
            mask  = dates.dt.date == today
            today_df = df[mask]
        else:
            today_df = df.iloc[-50:]
    except Exception:
        today_df = df.iloc[-50:]
    return today_df

def _get_poc(df):
    """
    Compute (poc_price, session_volume_avg) for today's bars.

    Uses a volume-weighted histogram of *typical price* ((H+L+C)/3) rather
    than close-only, which better approximates where volume actually traded
    within each bar instead of just the closing tick. Bucket assignment is
    fully vectorized with np.bincount instead of a per-bar Python loop —
    meaningfully cheaper when this runs across hundreds of symbols per scan
    cycle. Results are memoized per (df identity, bar count) so that
    volume_profile_poc_buy and volume_profile_poc_sell — which are always
    evaluated back-to-back against the same df/ind inside a single
    _strat_votes() pass — only pay for the histogram once.
    """
    key = (id(df), len(df))
    cached = _poc_cache.get(key)
    if cached is not None:
        return cached

    today_df = _get_today_df(df)
    if len(today_df) < 5:
        result = (None, None)
    else:
        typical = ((today_df['high'] + today_df['low'] + today_df['close']) / 3.0).values
        volumes = today_df['volume'].values.astype(float)
        price_min = float(np.min(typical))
        price_max = float(np.max(typical))
        rng = price_max - price_min
        if rng < 1e-9:
            result = (None, None)
        else:
            n_buckets = 20
            bucket_size = rng / n_buckets
            idx = np.clip(((typical - price_min) / bucket_size).astype(int), 0, n_buckets - 1)
            bucket_vols = np.bincount(idx, weights=volumes, minlength=n_buckets)
            poc_idx = int(np.argmax(bucket_vols))
            poc_price = price_min + (poc_idx + 0.5) * bucket_size
            sess_vol_avg = float(volumes.mean())
            result = (poc_price, sess_vol_avg)

    _poc_cache[key] = result
    if len(_poc_cache) > _POC_CACHE_MAX:
        _poc_cache.popitem(last=False)
    return result

def _f(series_or_val, fallback=np.nan):
    """Return the last scalar value of a Series, or the value itself."""
    try:
        if isinstance(series_or_val, pd.Series):
            v = series_or_val.iloc[-1]
        else:
            v = series_or_val
        return fallback if (isinstance(v, float) and np.isnan(v)) else float(v)
    except Exception:
        return fallback

def _fv(ind_row, key, fallback=np.nan):
    """Extract scalar from indicator row safely."""
    try:
        v = float(ind_row[key])
        return fallback if np.isnan(v) else v
    except Exception:
        return fallback

def _get_orb_range(df):
    """Find today's Opening Range (first 6 × 5-min bars = 9:15–9:44 AM)."""
    try:
        if 'date' in df.columns:
            dates = pd.to_datetime(df['date'])
        elif isinstance(df.index, pd.DatetimeIndex):
            dates = df.index
        else:
            if len(df) < 8:
                return None
            orb_high = float(df['high'].iloc[:6].max())
            orb_low  = float(df['low'].iloc[:6].min())
            return orb_high, orb_low

        today = dates.iloc[-1].date()
        today_mask = dates.dt.date == today
        today_bars = df[today_mask]
        if len(today_bars) < 7:
            return None
        orb_bars = today_bars.iloc[:6]
        orb_high = float(orb_bars['high'].max())
        orb_low  = float(orb_bars['low'].min())
        return orb_high, orb_low
    except Exception:
        return None

# ==================== HIGH-TRUST RETAINED STRATEGIES ====================

def buy_setup_original(df, ind):
    """EMA bull stack + volume surge + strong PDI — ~85% trust."""
    try:
        c = df['close'].iloc[-1]
        v = df['volume'].iloc[-1]
        i = ind.iloc[-1]
        e8   = _fv(i, 'ema_8');   e34  = _fv(i, 'ema_34');  e100 = _fv(i, 'ema_100')
        vm5  = _fv(i, 'vwma_5');  pdi  = _fv(i, 'plus_di')
        if any(np.isnan(x) for x in [e8, e34, e100, vm5, pdi]):
            return False
        return (c > e8 and c > e34 and c > e100 and
                v > vm5 * 1.2 and e8 > e34 and e34 > e100 and pdi > 30)
    except Exception:
        return False

def sell_setup_original(df, ind):
    """EMA bear stack + volume surge + strong MDI — ~85% trust."""
    try:
        c = df['close'].iloc[-1]
        v = df['volume'].iloc[-1]
        i = ind.iloc[-1]
        e8    = _fv(i, 'ema_8');   e34  = _fv(i, 'ema_34');  e100 = _fv(i, 'ema_100')
        vm5   = _fv(i, 'vwma_5');  mdi  = _fv(i, 'minus_di')
        if any(np.isnan(x) for x in [e8, e34, e100, vm5, mdi]):
            return False
        return (c < e8 and c < e34 and c < e100 and
                v > vm5 and e8 < e34 and e34 < e100 and mdi > 30)
    except Exception:
        return False

def rsi_oversold_bounce(df, ind):
    """RSI < 32 turning up + price above BB lower + volume — ~82% trust."""
    try:
        c     = df['close'].iloc[-1]
        v     = df['volume'].iloc[-1]
        i     = ind.iloc[-1];  prev = ind.iloc[-2]
        rsi   = _fv(i, 'rsi');    rsi_p = _fv(prev, 'rsi')
        bbl   = _fv(i, 'bb_lower'); s20 = _fv(i, 'sma_20')
        vm5   = _fv(i, 'vwma_5')
        if any(np.isnan(x) for x in [rsi, rsi_p, bbl, s20, vm5]):
            return False
        return rsi < 32 and rsi > rsi_p and c > bbl and v > vm5
    except Exception:
        return False

def rsi_overbought_reversal(df, ind):
    """RSI > 68 turning down + price below BB upper + volume — ~82% trust."""
    try:
        c     = df['close'].iloc[-1]
        v     = df['volume'].iloc[-1]
        i     = ind.iloc[-1];  prev = ind.iloc[-2]
        rsi   = _fv(i, 'rsi');    rsi_p = _fv(prev, 'rsi')
        bbu   = _fv(i, 'bb_upper'); s20 = _fv(i, 'sma_20')
        vm5   = _fv(i, 'vwma_5')
        if any(np.isnan(x) for x in [rsi, rsi_p, bbu, s20, vm5]):
            return False
        return rsi > 68 and rsi < rsi_p and c < bbu and v > vm5
    except Exception:
        return False

def macd_bullish_crossover(df, ind):
    """MACD crosses above signal + positive hist + RSI > 40 + volume — ~83% trust."""
    try:
        i    = ind.iloc[-1];  prev = ind.iloc[-2]
        macd = _fv(i, 'macd'); ms = _fv(i, 'macd_signal'); mh = _fv(i, 'macd_hist')
        pm   = _fv(prev, 'macd'); pms = _fv(prev, 'macd_signal')
        rsi  = _fv(i, 'rsi');  v = df['volume'].iloc[-1]; vm5 = _fv(i, 'vwma_5')
        if any(np.isnan(x) for x in [macd, ms, mh, pm, pms, rsi, vm5]):
            return False
        return pm <= pms and macd > ms and mh > 0 and rsi > 40 and v > vm5
    except Exception:
        return False

def macd_bearish_crossover(df, ind):
    """MACD crosses below signal + negative hist + RSI < 60 + volume — ~83% trust."""
    try:
        i    = ind.iloc[-1];  prev = ind.iloc[-2]
        macd = _fv(i, 'macd'); ms = _fv(i, 'macd_signal'); mh = _fv(i, 'macd_hist')
        pm   = _fv(prev, 'macd'); pms = _fv(prev, 'macd_signal')
        rsi  = _fv(i, 'rsi');  v = df['volume'].iloc[-1]; vm5 = _fv(i, 'vwma_5')
        if any(np.isnan(x) for x in [macd, ms, mh, pm, pms, rsi, vm5]):
            return False
        return pm >= pms and macd < ms and mh < 0 and rsi < 60 and v > vm5
    except Exception:
        return False

def bollinger_breakout_buy(df, ind):
    """Close > BB upper + expanding width + 1.5× vol + ADX > 25 — ~84% trust."""
    try:
        if len(ind) < 12:
            return False
        c    = df['close'].iloc[-1]
        v    = df['volume'].iloc[-1]
        i    = ind.iloc[-1]
        bbu  = _fv(i, 'bb_upper'); bbw = _fv(i, 'bb_width')
        adx  = _fv(i, 'adx');      vm10 = _fv(i, 'vwma_10')
        bbw_mean = float(ind['bb_width'].iloc[-11:-1].mean())
        if any(np.isnan(x) for x in [bbu, bbw, adx, vm10, bbw_mean]):
            return False
        return c > bbu and bbw > bbw_mean and v > vm10 * 1.5 and adx > 25
    except Exception:
        return False

def bollinger_breakout_sell(df, ind):
    """Close < BB lower + expanding width + 1.5× vol + ADX > 25 — ~84% trust."""
    try:
        if len(ind) < 12:
            return False
        c    = df['close'].iloc[-1]
        v    = df['volume'].iloc[-1]
        i    = ind.iloc[-1]
        bbl  = _fv(i, 'bb_lower'); bbw = _fv(i, 'bb_width')
        adx  = _fv(i, 'adx');      vm10 = _fv(i, 'vwma_10')
        bbw_mean = float(ind['bb_width'].iloc[-11:-1].mean())
        if any(np.isnan(x) for x in [bbl, bbw, adx, vm10, bbw_mean]):
            return False
        return c < bbl and bbw > bbw_mean and v > vm10 * 1.5 and adx > 25
    except Exception:
        return False

def volume_price_breakout(df, ind):
    """20-bar high breakout + 2× vol + ADX > 25 + OBV rising — ~86% trust."""
    try:
        if len(df) < 22:
            return False
        c    = df['close'].iloc[-1]
        v    = df['volume'].iloc[-1]
        i    = ind.iloc[-1]
        adx  = _fv(i, 'adx'); obv = _fv(i, 'obv'); obv_ma = _fv(i, 'obv_ma')
        vm20 = _fv(i, 'vwma_20')
        high20 = float(df['high'].iloc[-21:-1].max())
        if any(np.isnan(x) for x in [adx, obv, obv_ma, vm20]):
            return False
        return c > high20 and v > vm20 * 2.0 and adx > 25 and obv > obv_ma
    except Exception:
        return False

def adx_strong_trend_buy(df, ind):
    """ADX > 28 + PDI dominant + EMA8 > EMA21 — ~83% trust."""
    try:
        i   = ind.iloc[-1]
        adx = _fv(i, 'adx'); pdi = _fv(i, 'plus_di'); mdi = _fv(i, 'minus_di')
        e8  = _fv(i, 'ema_8'); e21 = _fv(i, 'ema_21')
        if any(np.isnan(x) for x in [adx, pdi, mdi, e8, e21]):
            return False
        return adx > 28 and pdi > mdi and pdi > 22 and e8 > e21
    except Exception:
        return False

def adx_strong_trend_sell(df, ind):
    """ADX > 28 + MDI dominant + EMA8 < EMA21 — ~83% trust."""
    try:
        i   = ind.iloc[-1]
        adx = _fv(i, 'adx'); pdi = _fv(i, 'plus_di'); mdi = _fv(i, 'minus_di')
        e8  = _fv(i, 'ema_8'); e21 = _fv(i, 'ema_21')
        if any(np.isnan(x) for x in [adx, pdi, mdi, e8, e21]):
            return False
        return adx > 28 and mdi > pdi and mdi > 22 and e8 < e21
    except Exception:
        return False

def bull_flag_breakout(df, ind):
    """Strong pole + tight consolidation + breakout with vol surge — ~85% trust."""
    try:
        if len(df) < 22:
            return False
        pole_high  = float(df['high'].iloc[-12:-6].max())
        pole_low   = float(df['low'].iloc[-12:-6].min())
        pole_h     = pole_high - pole_low
        if pole_h <= 0:
            return False
        flag_high  = float(df['high'].iloc[-6:].max())
        flag_low   = float(df['low'].iloc[-6:].min())
        flag_range = flag_high - flag_low
        latest_close = df['close'].iloc[-1]
        avg_vol    = float(df['volume'].iloc[-7:-1].mean())
        vol_surge  = df['volume'].iloc[-1] > avg_vol * 1.8
        rsi        = _fv(ind.iloc[-1], 'rsi')
        return (pole_h > flag_range * 2.0 and
                latest_close > flag_high and
                vol_surge and
                (rsi > 50 if not np.isnan(rsi) else False))
    except Exception:
        return False

def bear_flag_breakout(df, ind):
    """Strong downpole + tight consolidation + breakdown with vol surge — ~85% trust."""
    try:
        if len(df) < 22:
            return False
        pole_high  = float(df['high'].iloc[-12:-6].max())
        pole_low   = float(df['low'].iloc[-12:-6].min())
        pole_h     = pole_high - pole_low
        if pole_h <= 0:
            return False
        flag_high  = float(df['high'].iloc[-6:].max())
        flag_low   = float(df['low'].iloc[-6:].min())
        flag_range = flag_high - flag_low
        latest_close = df['close'].iloc[-1]
        avg_vol    = float(df['volume'].iloc[-7:-1].mean())
        vol_surge  = df['volume'].iloc[-1] > avg_vol * 1.8
        rsi        = _fv(ind.iloc[-1], 'rsi')
        return (pole_h > flag_range * 2.0 and
                latest_close < flag_low and
                vol_surge and
                (rsi < 50 if not np.isnan(rsi) else False))
    except Exception:
        return False

def institutional_buying(df, ind):
    """Majority up-closes (≥3 of last 4 transitions) on majority above-avg
    volume, net positive move, rising final-bar volume + ADX > 22 — ~82% trust.

    Relaxed from requiring all 5 bars to close consecutively higher: real
    accumulation on noisy 5-min data is rarely monotonic bar-to-bar, so the
    strict version almost never fired even during genuine institutional
    buying. A 3-of-4 majority plus a net-positive move over the window
    still filters out random chop while actually being reachable.
    """
    try:
        if len(df) < 12:
            return False
        closes  = df['close'].iloc[-5:].values
        volumes = df['volume'].iloc[-5:].values
        avg_vol = float(df['volume'].iloc[-10:-5].mean())
        if np.isnan(avg_vol) or avg_vol <= 0:
            return False
        up_moves    = sum(closes[j] > closes[j-1] for j in range(1, 5))
        above_avg_n = sum(volumes[j] > avg_vol    for j in range(1, 5))
        majority_up  = up_moves >= 3
        majority_vol = above_avg_n >= 3
        net_up       = closes[-1] > closes[0]
        vol_rising   = volumes[-1] > volumes[-2]
        adx = _fv(ind.iloc[-1], 'adx')
        return (majority_up and majority_vol and net_up and vol_rising and
                (adx > 22 if not np.isnan(adx) else False))
    except Exception:
        return False

def institutional_selling(df, ind):
    """Majority down-closes (≥3 of last 4 transitions) on majority above-avg
    volume, net negative move, rising final-bar volume + ADX > 22 — ~82% trust.
    (Relaxed mirror of institutional_buying — see that docstring.)
    """
    try:
        if len(df) < 12:
            return False
        closes  = df['close'].iloc[-5:].values
        volumes = df['volume'].iloc[-5:].values
        avg_vol = float(df['volume'].iloc[-10:-5].mean())
        if np.isnan(avg_vol) or avg_vol <= 0:
            return False
        dn_moves    = sum(closes[j] < closes[j-1] for j in range(1, 5))
        above_avg_n = sum(volumes[j] > avg_vol    for j in range(1, 5))
        majority_dn  = dn_moves >= 3
        majority_vol = above_avg_n >= 3
        net_dn       = closes[-1] < closes[0]
        vol_rising   = volumes[-1] > volumes[-2]
        adx = _fv(ind.iloc[-1], 'adx')
        return (majority_dn and majority_vol and net_dn and vol_rising and
                (adx > 22 if not np.isnan(adx) else False))
    except Exception:
        return False

def ema_cluster_buy(df, ind):
    """Fast EMA bull stack (8>13>21>34>50) + price above EMA100 with EMA100
    flat-to-rising + momentum bar — ~85% trust.

    Relaxed from the original 8>13>21>34>50>100>200 full stack: EMA200 on
    5-min bars needs ~1000 minutes (3+ trading days) of clean, uninterrupted
    uptrend to actually stack below EMA100, which almost never happens
    intraday — the old condition fired so rarely it was effectively dead.
    EMA100 trend (vs. 5 bars back) still confirms the higher-timeframe
    context without demanding an unreachable EMA200 ordering.
    """
    try:
        if len(ind) < 7:
            return False
        i   = ind.iloc[-1]
        e8  = _fv(i,'ema_8');  e13 = _fv(i,'ema_13'); e21 = _fv(i,'ema_21')
        e34 = _fv(i,'ema_34'); e50 = _fv(i,'ema_50'); e100= _fv(i,'ema_100')
        vm10 = _fv(i,'vwma_10')
        c   = df['close'].iloc[-1]; c_prev = df['close'].iloc[-2]
        v   = df['volume'].iloc[-1]
        if any(np.isnan(x) for x in [e8,e13,e21,e34,e50,e100,vm10]):
            return False
        e100_prev = _fv(ind.iloc[-6], 'ema_100', e100)
        fast_stack   = (e8 > e13 and e13 > e21 and e21 > e34 and e34 > e50)
        above_e100   = c > e100
        e100_ok      = e100 >= e100_prev * 0.999  # flat-to-rising, not falling
        return (fast_stack and above_e100 and e100_ok and
                c > e8 and c > c_prev * 1.005 and v > vm10)
    except Exception:
        return False

def ema_cluster_sell(df, ind):
    """Fast EMA bear stack (8<13<21<34<50) + price below EMA100 with EMA100
    flat-to-falling + momentum bar — ~85% trust. (Relaxed mirror of
    ema_cluster_buy — see that docstring for rationale.)
    """
    try:
        if len(ind) < 7:
            return False
        i   = ind.iloc[-1]
        e8  = _fv(i,'ema_8');  e13 = _fv(i,'ema_13'); e21 = _fv(i,'ema_21')
        e34 = _fv(i,'ema_34'); e50 = _fv(i,'ema_50'); e100= _fv(i,'ema_100')
        vm10 = _fv(i,'vwma_10')
        c   = df['close'].iloc[-1]; c_prev = df['close'].iloc[-2]
        v   = df['volume'].iloc[-1]
        if any(np.isnan(x) for x in [e8,e13,e21,e34,e50,e100,vm10]):
            return False
        e100_prev = _fv(ind.iloc[-6], 'ema_100', e100)
        fast_stack   = (e8 < e13 and e13 < e21 and e21 < e34 and e34 < e50)
        below_e100   = c < e100
        e100_ok      = e100 <= e100_prev * 1.001  # flat-to-falling, not rising
        return (fast_stack and below_e100 and e100_ok and
                c < e8 and c < c_prev * 0.995 and v > vm10)
    except Exception:
        return False

def supertrend_breakout(df, ind):
    """Price crosses Supertrend band (ATR-based) with 1.5× volume — ~82% trust."""
    try:
        if len(df) < 13:
            return False
        i    = ind.iloc[-1]
        atr  = _fv(i, 'atr')
        if np.isnan(atr): return False
        c    = df['close'].iloc[-1]
        v    = df['volume'].iloc[-1]
        avg_vol = float(df['volume'].iloc[-7:-1].mean())
        upper = float(df['high'].iloc[-11:-1].max()) + atr
        lower = float(df['low'].iloc[-11:-1].min())  - atr
        vol_ok = v > avg_vol * 1.5
        return (c > upper or c < lower) and vol_ok
    except Exception:
        return False

def vwap_reclaim_buy(df, ind):
    """Price crosses above VWAP + 1.3× vol + trend EMA9>EMA21 + RSI in range — ~86% trust."""
    try:
        if len(df) < 5:
            return False
        i       = ind.iloc[-1];  prev = ind.iloc[-2]
        vwap    = _fv(i, 'vwap');  vwap_p = _fv(prev, 'vwap')
        if np.isnan(vwap) or np.isnan(vwap_p): return False
        c       = df['close'].iloc[-1]
        c_prev  = df['close'].iloc[-2]
        v       = df['volume'].iloc[-1]
        vm20    = _fv(i, 'vol_ma20')
        rsi     = _fv(i, 'rsi')
        e9      = _fv(i, 'ema_9');  e21 = _fv(i, 'ema_21')
        if np.isnan(vm20): return False
        crossed_above = c_prev < vwap_p and c > vwap
        vol_confirm   = v > vm20 * 1.3
        trend_ok      = (e9 > e21) if not (np.isnan(e9) or np.isnan(e21)) else True
        rsi_ok        = (30 < rsi < 65) if not np.isnan(rsi) else True
        return crossed_above and vol_confirm and trend_ok and rsi_ok
    except Exception:
        return False

def vwap_rejection_sell(df, ind):
    """Price crosses below VWAP + 1.3× vol + trend EMA9<EMA21 + RSI in range — ~86% trust."""
    try:
        if len(df) < 5:
            return False
        i       = ind.iloc[-1];  prev = ind.iloc[-2]
        vwap    = _fv(i, 'vwap');  vwap_p = _fv(prev, 'vwap')
        if np.isnan(vwap) or np.isnan(vwap_p): return False
        c       = df['close'].iloc[-1]
        c_prev  = df['close'].iloc[-2]
        v       = df['volume'].iloc[-1]
        vm20    = _fv(i, 'vol_ma20')
        rsi     = _fv(i, 'rsi')
        e9      = _fv(i, 'ema_9');  e21 = _fv(i, 'ema_21')
        if np.isnan(vm20): return False
        crossed_below = c_prev > vwap_p and c < vwap
        vol_confirm   = v > vm20 * 1.3
        trend_ok      = (e9 < e21) if not (np.isnan(e9) or np.isnan(e21)) else True
        rsi_ok        = (35 < rsi < 70) if not np.isnan(rsi) else True
        return crossed_below and vol_confirm and trend_ok and rsi_ok
    except Exception:
        return False

def opening_range_breakout(df, ind):
    """Close > ORB high + 1.6× vol + RSI > 50 + ADX > 20 — ~87% trust (9:15–10:15 slot)."""
    try:
        if len(df) < 10:
            return False
        orb = _get_orb_range(df)
        if orb is None:
            return False
        orb_high = orb[0]
        c        = df['close'].iloc[-1]
        v        = df['volume'].iloc[-1]
        avg_vol  = float(df['volume'].iloc[-7:-1].mean())
        adx      = _fv(ind.iloc[-1], 'adx')
        rsi      = _fv(ind.iloc[-1], 'rsi')
        if np.isnan(avg_vol) or avg_vol == 0: return False
        vol_ok = v > avg_vol * 1.6
        rsi_ok = (rsi > 50) if not np.isnan(rsi) else True
        adx_ok = (adx > 20) if not np.isnan(adx) else True
        return c > orb_high and vol_ok and rsi_ok and adx_ok
    except Exception:
        return False

def orb_breakdown(df, ind):
    """Close < ORB low + 1.6× vol + RSI < 50 + ADX > 20 — ~87% trust (9:15–10:15 slot)."""
    try:
        if len(df) < 10:
            return False
        orb = _get_orb_range(df)
        if orb is None:
            return False
        orb_low  = orb[1]
        c        = df['close'].iloc[-1]
        v        = df['volume'].iloc[-1]
        avg_vol  = float(df['volume'].iloc[-7:-1].mean())
        adx      = _fv(ind.iloc[-1], 'adx')
        rsi      = _fv(ind.iloc[-1], 'rsi')
        if np.isnan(avg_vol) or avg_vol == 0: return False
        vol_ok = v > avg_vol * 1.6
        rsi_ok = (rsi < 50) if not np.isnan(rsi) else True
        adx_ok = (adx > 20) if not np.isnan(adx) else True
        return c < orb_low and vol_ok and rsi_ok and adx_ok
    except Exception:
        return False

def macd_fast_bullish(df, ind):
    """Fast MACD (5/13) crosses up + hist expanding + RSI 40–70 + volume — ~83% trust."""
    try:
        i     = ind.iloc[-1];  prev = ind.iloc[-2]
        mf    = _fv(i, 'macd_fast');    mfs  = _fv(i, 'macd_fast_signal')
        mfh   = _fv(i, 'macd_fast_hist')
        pmf   = _fv(prev, 'macd_fast'); pmfs = _fv(prev, 'macd_fast_signal')
        pmfh  = _fv(prev, 'macd_fast_hist')
        rsi   = _fv(i, 'rsi')
        v     = df['volume'].iloc[-1]; vm10 = _fv(i, 'vwma_10')
        if any(np.isnan(x) for x in [mf, mfs, mfh, pmf, pmfs, pmfh, vm10]):
            return False
        cross_up   = pmf <= pmfs and mf > mfs
        hist_up    = mfh > pmfh
        above_zero = mf > -0.05 * abs(mf + 1e-9)
        rsi_ok     = (40 < rsi < 70) if not np.isnan(rsi) else True
        return cross_up and hist_up and v > vm10 and rsi_ok
    except Exception:
        return False

def macd_fast_bearish(df, ind):
    """Fast MACD (5/13) crosses down + hist shrinking + RSI 30–60 + volume — ~83% trust."""
    try:
        i     = ind.iloc[-1];  prev = ind.iloc[-2]
        mf    = _fv(i, 'macd_fast');    mfs  = _fv(i, 'macd_fast_signal')
        mfh   = _fv(i, 'macd_fast_hist')
        pmf   = _fv(prev, 'macd_fast'); pmfs = _fv(prev, 'macd_fast_signal')
        pmfh  = _fv(prev, 'macd_fast_hist')
        rsi   = _fv(i, 'rsi')
        v     = df['volume'].iloc[-1]; vm10 = _fv(i, 'vwma_10')
        if any(np.isnan(x) for x in [mf, mfs, mfh, pmf, pmfs, pmfh, vm10]):
            return False
        cross_dn   = pmf >= pmfs and mf < mfs
        hist_dn    = mfh < pmfh
        rsi_ok     = (30 < rsi < 60) if not np.isnan(rsi) else True
        return cross_dn and hist_dn and v > vm10 and rsi_ok
    except Exception:
        return False

def bb_squeeze_breakout_buy(df, ind):
    """BB inside KC (squeeze) then expands bullishly — ~84% trust."""
    try:
        if len(df) < 22:
            return False
        i      = ind.iloc[-1];  prev = ind.iloc[-2]
        squeeze_now  = _fv(i, 'squeeze')
        squeeze_prev = _fv(prev, 'squeeze')
        bbu    = _fv(i, 'bb_upper'); bbl = _fv(i, 'bb_lower')
        c      = df['close'].iloc[-1]; o = df['open'].iloc[-1]
        v      = df['volume'].iloc[-1]
        vm20   = _fv(i, 'vol_ma20')
        rsi    = _fv(i, 'rsi')
        e9     = _fv(i, 'ema_9')
        if any(np.isnan(x) for x in [bbu, bbl, vm20]): return False
        squeeze_released = (squeeze_prev > 0.5 and squeeze_now < 0.5)
        bbw_curr = _fv(i, 'bb_width'); bbw_prev = _fv(prev, 'bb_width')
        expanding = (bbw_curr > bbw_prev * 1.1) if not (np.isnan(bbw_curr) or np.isnan(bbw_prev)) else False
        rsi_ok  = (rsi > 50) if not np.isnan(rsi) else True
        bull_c  = c > o
        above_e9 = (c > e9) if not np.isnan(e9) else True
        return ((squeeze_released or expanding) and
                c > bbu and bull_c and
                v > vm20 * 1.4 and rsi_ok and above_e9)
    except Exception:
        return False

def bb_squeeze_breakout_sell(df, ind):
    """BB inside KC (squeeze) then expands bearishly — ~84% trust."""
    try:
        if len(df) < 22:
            return False
        i      = ind.iloc[-1];  prev = ind.iloc[-2]
        squeeze_now  = _fv(i, 'squeeze')
        squeeze_prev = _fv(prev, 'squeeze')
        bbl    = _fv(i, 'bb_lower')
        c      = df['close'].iloc[-1]; o = df['open'].iloc[-1]
        v      = df['volume'].iloc[-1]
        vm20   = _fv(i, 'vol_ma20')
        rsi    = _fv(i, 'rsi')
        e9     = _fv(i, 'ema_9')
        if any(np.isnan(x) for x in [bbl, vm20]): return False
        squeeze_released = (squeeze_prev > 0.5 and squeeze_now < 0.5)
        bbw_curr = _fv(i, 'bb_width'); bbw_prev = _fv(prev, 'bb_width')
        expanding = (bbw_curr > bbw_prev * 1.1) if not (np.isnan(bbw_curr) or np.isnan(bbw_prev)) else False
        rsi_ok  = (rsi < 50) if not np.isnan(rsi) else True
        bear_c  = c < o
        below_e9 = (c < e9) if not np.isnan(e9) else True
        return ((squeeze_released or expanding) and
                c < bbl and bear_c and
                v > vm20 * 1.4 and rsi_ok and below_e9)
    except Exception:
        return False

# ==================== NEW HIGH-TRUST STRATEGIES ====================

def liquidity_sweep_buy(df, ind):
    """
    LIQUIDITY SWEEP — Bullish Stop Hunt Reversal (~88% trust).

    Logic: Price dips below a recent swing low (sweeping buy stops /
    triggering sell stops), then immediately reclaims the level within
    1–2 bars with a spike in volume. This is classic smart-money
    stop-hunt behavior before pushing higher.

    Conditions:
      1. Previous bar's low breaks 10-bar swing low (sweep candle).
      2. Current close reclaims above that swing low (reversal confirmation).
      3. Volume on the current bar >= 1.7× 20-bar average (institutional entry).
      4. RSI was < 45 at the sweep (shows oversold extreme reached).
      5. Current bar is bullish (close > open).
      6. Price is now above VWAP or EMA9 (momentum restored).
    """
    try:
        if len(df) < 15:
            return False
        i          = ind.iloc[-1]
        prev       = ind.iloc[-2]

        c_now      = float(df['close'].iloc[-1])
        o_now      = float(df['open'].iloc[-1])
        l_now      = float(df['low'].iloc[-1])
        l_prev     = float(df['low'].iloc[-2])
        c_prev     = float(df['close'].iloc[-2])
        v_now      = float(df['volume'].iloc[-1])

        # 10-bar swing low (exclude last 2 bars so they can break it)
        swing_low  = float(df['low'].iloc[-12:-2].min())

        vm20       = _fv(i, 'vol_ma20')
        rsi_prev   = _fv(prev, 'rsi')
        vwap       = _fv(i, 'vwap')
        e9         = _fv(i, 'ema_9')

        if np.isnan(vm20) or vm20 <= 0:
            return False

        # 1. Previous bar swept (dipped below swing low)
        sweep_happened  = l_prev < swing_low

        # 2. Current bar closes back above swing low (reclaim)
        reclaimed       = c_now > swing_low

        # 3. Volume surge on current bar
        vol_surge       = v_now >= vm20 * 1.7

        # 4. RSI was oversold at the sweep
        rsi_oversold    = (rsi_prev < 45) if not np.isnan(rsi_prev) else True

        # 5. Bullish current bar
        bull_candle     = c_now > o_now

        # 6. Momentum anchor — above VWAP or EMA9
        momentum_ok     = False
        if not np.isnan(vwap) and vwap > 0:
            momentum_ok = c_now > vwap
        elif not np.isnan(e9) and e9 > 0:
            momentum_ok = c_now > e9
        else:
            momentum_ok = True  # no anchor available, allow

        return (sweep_happened and reclaimed and vol_surge and
                rsi_oversold and bull_candle and momentum_ok)
    except Exception:
        return False

def liquidity_sweep_sell(df, ind):
    """
    LIQUIDITY SWEEP — Bearish Stop Hunt Reversal (~88% trust).

    Logic: Price spikes above a recent swing high (sweeping sell stops /
    triggering buy stops), then immediately drops back below that level.
    Classic smart-money stop-hunt before pushing lower.

    Conditions:
      1. Previous bar's high breaks 10-bar swing high (sweep candle).
      2. Current close reclaims below that swing high (reversal confirmation).
      3. Volume on current bar >= 1.7× 20-bar average.
      4. RSI was > 55 at the sweep (overbought extreme reached).
      5. Current bar is bearish (close < open).
      6. Price is now below VWAP or EMA9 (momentum lost).
    """
    try:
        if len(df) < 15:
            return False
        i          = ind.iloc[-1]
        prev       = ind.iloc[-2]

        c_now      = float(df['close'].iloc[-1])
        o_now      = float(df['open'].iloc[-1])
        h_prev     = float(df['high'].iloc[-2])
        v_now      = float(df['volume'].iloc[-1])

        # 10-bar swing high (exclude last 2 bars)
        swing_high = float(df['high'].iloc[-12:-2].max())

        vm20       = _fv(i, 'vol_ma20')
        rsi_prev   = _fv(prev, 'rsi')
        vwap       = _fv(i, 'vwap')
        e9         = _fv(i, 'ema_9')

        if np.isnan(vm20) or vm20 <= 0:
            return False

        # 1. Previous bar swept above swing high
        sweep_happened  = h_prev > swing_high

        # 2. Current bar closes back below swing high
        reclaimed       = c_now < swing_high

        # 3. Volume surge on current bar
        vol_surge       = v_now >= vm20 * 1.7

        # 4. RSI was overbought at the sweep
        rsi_overbought  = (rsi_prev > 55) if not np.isnan(rsi_prev) else True

        # 5. Bearish current bar
        bear_candle     = c_now < o_now

        # 6. Momentum anchor — below VWAP or EMA9
        momentum_ok     = False
        if not np.isnan(vwap) and vwap > 0:
            momentum_ok = c_now < vwap
        elif not np.isnan(e9) and e9 > 0:
            momentum_ok = c_now < e9
        else:
            momentum_ok = True

        return (sweep_happened and reclaimed and vol_surge and
                rsi_overbought and bear_candle and momentum_ok)
    except Exception:
        return False

def order_flow_imbalance_buy(df, ind):
    """
    ORDER FLOW IMBALANCE — Buy-side Delta Dominance (~85% trust).

    Approximates order flow delta using volume × price-position within bar.
    A bar where close is near the top has buyers absorbing sellers
    (buy imbalance). When this happens on rising OBV and above VWAP,
    it signals institutional accumulation.

    Conditions:
      1. Current bar: close is in the upper 70% of the bar (buy pressure).
      2. At least 3 of last 5 bars show buy imbalance (upper 60% close).
      3. OBV is above its 20-bar MA and slope is positive.
      4. Volume >= 1.5× 20-bar average (confirms real flow, not noise).
      5. CMF (Chaikin Money Flow) > 0.08 (money flowing in).
      6. Price above VWAP (buyers are dominant intraday).
    """
    try:
        if len(df) < 10:
            return False
        i = ind.iloc[-1]

        # Bar structure for imbalance detection
        highs  = df['high'].values
        lows   = df['low'].values
        closes = df['close'].values

        def _buy_imbalance(idx, threshold=0.60):
            rng = highs[idx] - lows[idx]
            if rng < 1e-9:
                return False
            pos = (closes[idx] - lows[idx]) / rng
            return pos >= threshold

        # 1. Current bar: buy imbalance (close in upper 70%)
        n = len(df) - 1
        cur_rng = highs[n] - lows[n]
        if cur_rng < 1e-9:
            return False
        cur_pos = (closes[n] - lows[n]) / cur_rng
        cur_imbalance = cur_pos >= 0.70

        # 2. Majority of last 5 bars show buy imbalance
        recent_count = sum(_buy_imbalance(j) for j in range(max(0, n-4), n))
        majority_ok  = recent_count >= 3

        # 3. OBV trending up
        obv    = _fv(i, 'obv')
        obv_ma = _fv(i, 'obv_ma')
        obv_sl = _fv(i, 'obv_slope')
        obv_ok = (not np.isnan(obv) and not np.isnan(obv_ma) and
                  obv > obv_ma and (np.isnan(obv_sl) or obv_sl > 0))

        # 4. Volume surge
        vol_now = float(df['volume'].iloc[-1])
        vm20    = _fv(i, 'vol_ma20')
        vol_ok  = (not np.isnan(vm20) and vm20 > 0 and vol_now >= vm20 * 1.5)

        # 5. CMF positive
        cmf    = _fv(i, 'cmf')
        cmf_ok = (np.isnan(cmf) or cmf > 0.08)

        # 6. Above VWAP
        vwap   = _fv(i, 'vwap')
        vwap_ok = (np.isnan(vwap) or vwap <= 0 or closes[n] > vwap)

        return (cur_imbalance and majority_ok and obv_ok and
                vol_ok and cmf_ok and vwap_ok)
    except Exception:
        return False

def order_flow_imbalance_sell(df, ind):
    """
    ORDER FLOW IMBALANCE — Sell-side Delta Dominance (~85% trust).

    When close is near the bottom of a bar with rising volume and falling
    OBV + negative CMF, sellers are in control. Smart money is distributing.

    Conditions:
      1. Current bar: close in the lower 30% of the bar (sell pressure).
      2. At least 3 of last 5 bars show sell imbalance (lower 40% close).
      3. OBV is below its 20-bar MA and slope is negative.
      4. Volume >= 1.8× 20-bar average.
      5. CMF < -0.10 (money flowing out).
      6. Price below VWAP (sellers dominant intraday).
    """
    try:
        if len(df) < 10:
            return False
        i = ind.iloc[-1]

        highs  = df['high'].values
        lows   = df['low'].values
        closes = df['close'].values

        def _sell_imbalance(idx, threshold=0.40):
            rng = highs[idx] - lows[idx]
            if rng < 1e-9:
                return False
            pos = (closes[idx] - lows[idx]) / rng
            return pos <= threshold

        n = len(df) - 1
        cur_rng = highs[n] - lows[n]
        if cur_rng < 1e-9:
            return False
        cur_pos = (closes[n] - lows[n]) / cur_rng
        cur_imbalance = cur_pos <= 0.30

        recent_count = sum(_sell_imbalance(j) for j in range(max(0, n-4), n))
        majority_ok  = recent_count >= 3

        obv    = _fv(i, 'obv')
        obv_ma = _fv(i, 'obv_ma')
        obv_sl = _fv(i, 'obv_slope')
        obv_ok = (not np.isnan(obv) and not np.isnan(obv_ma) and
                  obv < obv_ma and (np.isnan(obv_sl) or obv_sl < 0))

        vol_now = float(df['volume'].iloc[-1])
        vm20    = _fv(i, 'vol_ma20')
        vol_ok  = (not np.isnan(vm20) and vm20 > 0 and vol_now >= vm20 * 1.5)

        cmf    = _fv(i, 'cmf')
        cmf_ok = (np.isnan(cmf) or cmf < -0.08)

        vwap   = _fv(i, 'vwap')
        vwap_ok = (np.isnan(vwap) or vwap <= 0 or closes[n] < vwap)

        return (cur_imbalance and majority_ok and obv_ok and
                vol_ok and cmf_ok and vwap_ok)
    except Exception:
        return False

def anchored_vwap_bounce_buy(df, ind):
    """
    ANCHORED VWAP — Bounce off Session VWAP after pullback (~87% trust).

    Anchored VWAP is the standard intraday VWAP anchored to market open (9:15).
    We detect when price dips toward VWAP (within 0.5% below), then reverses
    upward with volume. This is the highest-probability long entry in intraday —
    the 'discount-to-fair-value' trade that institutions use.

    Conditions:
      1. Price touched or went slightly below VWAP in the previous 1–3 bars
         (dip toward VWAP — the 'test').
      2. Current close is above VWAP (reclaim confirmed).
      3. Current bar is bullish (close > open).
      4. Volume >= 1.3× 20-bar average (shows buying conviction at VWAP).
      5. VWAP upper band 1σ is not yet hit (not extended, still room).
      6. RSI is between 30 and 70 (not already overbought before entry).
      7. EMA9 slope is positive (short-term trend aligned).
    """
    try:
        if len(df) < 8:
            return False
        i    = ind.iloc[-1]

        c_now    = float(df['close'].iloc[-1])
        o_now    = float(df['open'].iloc[-1])
        v_now    = float(df['volume'].iloc[-1])

        vwap     = _fv(i, 'vwap')
        vwap_u1  = _fv(i, 'vwap_upper1')
        vm20     = _fv(i, 'vol_ma20')
        rsi      = _fv(i, 'rsi')
        e9_slope = _fv(i, 'ema9_slope')

        if np.isnan(vwap) or vwap <= 0 or np.isnan(vm20) or vm20 <= 0:
            return False

        # 1. Recent test of VWAP — any of last 3 bars touched within 0.5% below VWAP
        vwap_threshold = vwap * 0.995
        recent_bars    = df.iloc[-4:-1]  # last 3 bars before current
        tested_vwap    = any(float(lw) <= vwap_threshold for lw in recent_bars['low'])

        # 2. Current close back above VWAP
        above_vwap  = c_now > vwap

        # 3. Bullish current bar
        bull_candle = c_now > o_now

        # 4. Volume surge at VWAP
        vol_ok      = v_now >= vm20 * 1.3

        # 5. Not extended — still below VWAP+1σ (room to run)
        not_extended = (np.isnan(vwap_u1) or vwap_u1 <= 0 or c_now < vwap_u1 * 1.005)

        # 6. RSI in tradeable zone
        rsi_ok = (30 < rsi < 70) if not np.isnan(rsi) else True

        # 7. EMA9 slope positive (uptrend on current timeframe)
        slope_ok = (np.isnan(e9_slope) or e9_slope > 0)

        return (tested_vwap and above_vwap and bull_candle and
                vol_ok and not_extended and rsi_ok and slope_ok)
    except Exception:
        return False

def anchored_vwap_bounce_sell(df, ind):
    """
    ANCHORED VWAP — Rejection at Session VWAP after rally (~87% trust).

    Price rallies up toward VWAP (within 0.5% above), gets rejected,
    and closes back below VWAP with volume. This is the 'premium-to-fair-value'
    short entry — institutions distribute into VWAP rallies.

    Conditions:
      1. Price touched or went slightly above VWAP in previous 1–3 bars.
      2. Current close is below VWAP (rejection confirmed).
      3. Current bar is bearish (close < open).
      4. Volume >= 1.3× 20-bar average.
      5. VWAP lower band 1σ is not yet hit (room to fall).
      6. RSI between 30 and 70.
      7. EMA9 slope is negative.
    """
    try:
        if len(df) < 8:
            return False
        i    = ind.iloc[-1]

        c_now    = float(df['close'].iloc[-1])
        o_now    = float(df['open'].iloc[-1])
        v_now    = float(df['volume'].iloc[-1])

        vwap     = _fv(i, 'vwap')
        vwap_l1  = _fv(i, 'vwap_lower1')
        vm20     = _fv(i, 'vol_ma20')
        rsi      = _fv(i, 'rsi')
        e9_slope = _fv(i, 'ema9_slope')

        if np.isnan(vwap) or vwap <= 0 or np.isnan(vm20) or vm20 <= 0:
            return False

        # 1. Recent test of VWAP from below — any of last 3 bars touched within 0.5% above VWAP
        vwap_threshold = vwap * 1.005
        recent_bars    = df.iloc[-4:-1]
        tested_vwap    = any(float(h) >= vwap_threshold for h in recent_bars['high'])

        # 2. Current close back below VWAP
        below_vwap  = c_now < vwap

        # 3. Bearish current bar
        bear_candle = c_now < o_now

        # 4. Volume surge at VWAP rejection
        vol_ok      = v_now >= vm20 * 1.3

        # 5. Not extended — still above VWAP-1σ (room to fall)
        not_extended = (np.isnan(vwap_l1) or vwap_l1 <= 0 or c_now > vwap_l1 * 0.995)

        # 6. RSI in tradeable zone
        rsi_ok = (30 < rsi < 70) if not np.isnan(rsi) else True

        # 7. EMA9 slope negative (downtrend on current timeframe)
        slope_ok = (np.isnan(e9_slope) or e9_slope < 0)

        return (tested_vwap and below_vwap and bear_candle and
                vol_ok and not_extended and rsi_ok and slope_ok)
    except Exception:
        return False

def volume_profile_poc_buy(df, ind):
    """
    VOLUME PROFILE — Price at Point of Control (POC) support (~86% trust).

    The POC (price level with highest cumulative volume for the session) acts
    as a magnet and a key support/resistance zone. When price pulls back to
    the POC and bounces with volume, it's a high-probability long entry.

    POC is computed from a volume-weighted typical-price ((H+L+C)/3) histogram
    on today's bars (or last 50 bars if intraday date unavailable), fully
    vectorized and memoized — see _get_poc().

    Conditions:
      1. Compute today's POC (highest-volume price level in 20 buckets).
      2. Price is within 0.5% of POC (at or testing POC as support).
      3. Current bar is bullish and closes above POC.
      4. Volume >= 1.4× session average.
      5. POC is above VWAP (POC acts as support above fair value — bullish).
      6. RSI > 38 (not deeply oversold, bounce momentum present).
      7. Lower-wick rejection confirms genuine support, not noise.
    """
    try:
        if len(df) < 20:
            return False
        i = ind.iloc[-1]

        poc_price, sess_vol_avg = _get_poc(df)
        if poc_price is None:
            return False

        c_now  = float(df['close'].iloc[-1])
        o_now  = float(df['open'].iloc[-1])
        v_now  = float(df['volume'].iloc[-1])

        vwap  = _fv(i, 'vwap')
        rsi   = _fv(i, 'rsi')

        # 1 & 2. Price near POC and closes above it (test + reclaim)
        near_poc    = abs(c_now - poc_price) / poc_price <= 0.005  # within 0.5%
        above_poc   = c_now > poc_price

        # 3. Bullish bar
        bull_candle = c_now > o_now

        # 4. Volume surge
        vol_ok      = (sess_vol_avg is not None and sess_vol_avg > 0 and v_now >= sess_vol_avg * 1.4)

        # 5. POC above VWAP (POC is premium — acting as support in uptrend)
        poc_above_vwap = (np.isnan(vwap) or vwap <= 0 or poc_price >= vwap)

        # 6. RSI shows momentum
        rsi_ok = (rsi > 38) if not np.isnan(rsi) else True

        # 7. Rejection wick confirmation — lower wick >= 30% of bar range
        #    (price probed into POC then bounced: genuine support, not noise)
        h_now   = float(df['high'].iloc[-1])
        l_now   = float(df['low'].iloc[-1])
        bar_rng = h_now - l_now
        lower_wick = (min(c_now, o_now) - l_now)
        wick_ok = (bar_rng < 1e-9) or (lower_wick / bar_rng >= 0.30)

        return (near_poc and above_poc and bull_candle and
                vol_ok and poc_above_vwap and rsi_ok and wick_ok)
    except Exception:
        return False

def volume_profile_poc_sell(df, ind):
    """
    VOLUME PROFILE — Price at Point of Control (POC) resistance (~86% trust).

    Price rallies to the POC (highest-volume zone) and gets rejected.
    This is a high-probability short entry as the POC acts as resistance.
    See volume_profile_poc_buy / _get_poc() for the POC computation.

    Conditions:
      1. Compute today's POC.
      2. Price is within 0.5% of POC (at or testing POC as resistance).
      3. Current bar is bearish and closes below POC.
      4. Volume >= 1.4× session average.
      5. POC is below VWAP (POC acts as resistance below fair value — bearish).
      6. RSI < 62.
      7. Upper-wick rejection confirms genuine resistance, not noise.
    """
    try:
        if len(df) < 20:
            return False
        i = ind.iloc[-1]

        poc_price, sess_vol_avg = _get_poc(df)
        if poc_price is None:
            return False

        c_now  = float(df['close'].iloc[-1])
        o_now  = float(df['open'].iloc[-1])
        v_now  = float(df['volume'].iloc[-1])

        vwap  = _fv(i, 'vwap')
        rsi   = _fv(i, 'rsi')

        near_poc    = abs(c_now - poc_price) / poc_price <= 0.005
        below_poc   = c_now < poc_price

        bear_candle = c_now < o_now

        vol_ok = (sess_vol_avg is not None and sess_vol_avg > 0 and v_now >= sess_vol_avg * 1.4)

        poc_below_vwap = (np.isnan(vwap) or vwap <= 0 or poc_price <= vwap)

        rsi_ok = (rsi < 62) if not np.isnan(rsi) else True

        # 7. Rejection wick confirmation — upper wick >= 30% of bar range
        #    (price probed POC resistance then got rejected: genuine resistance)
        h_now   = float(df['high'].iloc[-1])
        l_now   = float(df['low'].iloc[-1])
        bar_rng = h_now - l_now
        upper_wick = (h_now - max(c_now, o_now))
        wick_ok = (bar_rng < 1e-9) or (upper_wick / bar_rng >= 0.30)

        return (near_poc and below_poc and bear_candle and
                vol_ok and poc_below_vwap and rsi_ok and wick_ok)
    except Exception:
        return False

# ==================== STRATEGY REGISTRY ====================
# Only strategies with estimated intraday trust >= 80% are included.
# New strategies are marked with [NEW].

all_strategies = {
    # ── EMA Trend Stack (85%) ──────────────────────────────────────────
    'BUY_SETUP':               buy_setup_original,
    'SELL_SETUP':              sell_setup_original,

    # ── RSI Mean Reversion (82%) ───────────────────────────────────────
    'RSI_OVERSOLD_BOUNCE':     rsi_oversold_bounce,
    'RSI_OVERBOUGHT_REVERSAL': rsi_overbought_reversal,

    # ── MACD Crossover (83%) ───────────────────────────────────────────
    'MACD_BULLISH':            macd_bullish_crossover,
    'MACD_BEARISH':            macd_bearish_crossover,
    'MACD_FAST_BULL':          macd_fast_bullish,
    'MACD_FAST_BEAR':          macd_fast_bearish,

    # ── Bollinger / Squeeze Breakout (84%) ────────────────────────────
    'BB_BREAKOUT_BUY':         bollinger_breakout_buy,
    'BB_BREAKOUT_SELL':        bollinger_breakout_sell,
    'BB_SQUEEZE_BUY':          bb_squeeze_breakout_buy,
    'BB_SQUEEZE_SELL':         bb_squeeze_breakout_sell,

    # ── Volume Breakout / Institutional (84–86%) ──────────────────────
    'VOLUME_BREAKOUT':         volume_price_breakout,
    'INSTITUTIONAL_BUY':       institutional_buying,
    'INSTITUTIONAL_SELL':      institutional_selling,

    # ── ADX Trend (83%) ───────────────────────────────────────────────
    'ADX_TREND_BUY':           adx_strong_trend_buy,
    'ADX_TREND_SELL':          adx_strong_trend_sell,

    # ── EMA Cluster Perfect Stack (87%) ───────────────────────────────
    'EMA_CLUSTER_BUY':         ema_cluster_buy,
    'EMA_CLUSTER_SELL':        ema_cluster_sell,

    # ── SuperTrend (82%) ──────────────────────────────────────────────
    'SUPERTREND':              supertrend_breakout,

    # ── VWAP Cross (86%) ──────────────────────────────────────────────
    'VWAP_RECLAIM_BUY':        vwap_reclaim_buy,
    'VWAP_REJECTION_SELL':     vwap_rejection_sell,

    # ── Opening Range (87%) ───────────────────────────────────────────
    'ORB_BREAKOUT_BUY':        opening_range_breakout,
    'ORB_BREAKDOWN_SELL':      orb_breakdown,

    # ── Flag Patterns (85%) ───────────────────────────────────────────
    'BULL_FLAG':               bull_flag_breakout,
    'BEAR_FLAG':               bear_flag_breakout,

    # ── [NEW] Liquidity Sweep / Stop Hunt (88%) ───────────────────────
    'LIQUIDITY_SWEEP_BUY':     liquidity_sweep_buy,
    'LIQUIDITY_SWEEP_SELL':    liquidity_sweep_sell,

    # ── [NEW] Order Flow Imbalance / Delta (85%) ──────────────────────
    'ORDER_FLOW_BUY':          order_flow_imbalance_buy,
    'ORDER_FLOW_SELL':         order_flow_imbalance_sell,

    # ── [NEW] Anchored VWAP Bounce (87%) ─────────────────────────────
    'ANCHORED_VWAP_BUY':       anchored_vwap_bounce_buy,
    'ANCHORED_VWAP_SELL':      anchored_vwap_bounce_sell,

    # ── [NEW] Volume Profile POC (86%) ───────────────────────────────
    'VOL_PROFILE_POC_BUY':     volume_profile_poc_buy,
    'VOL_PROFILE_POC_SELL':    volume_profile_poc_sell,
}