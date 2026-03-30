# strategies.py — v2.2  INTRADAY OPTIMIZED (5-min NSE)
# ============================================================
# CHANGES in v2.2:
#   • No structural changes – all strategies remain as in v2.1.
#   • All functions now consistently handle NaN and missing data.
#   • Strategy registry unchanged – 40 strategies available.
# ============================================================

import numpy as np
import pandas as pd

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

# ========== ORIGINAL 17 STRATEGIES ==========
def sell_setup_original(df, ind):
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

def buy_setup_original(df, ind):
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

def rsi_oversold_bounce(df, ind):
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

def golden_cross(df, ind):
    try:
        i    = ind.iloc[-1];  prev = ind.iloc[-2]
        e50  = _fv(i, 'ema_50');  e200 = _fv(i, 'ema_200');  e21 = _fv(i, 'ema_21')
        pe50 = _fv(prev, 'ema_50'); pe200 = _fv(prev, 'ema_200')
        rsi  = _fv(i, 'rsi');  v = df['volume'].iloc[-1]; vm10 = _fv(i, 'vwma_10')
        if any(np.isnan(x) for x in [e50, e200, e21, pe50, pe200, rsi, vm10]):
            return False
        return pe50 <= pe200 and e50 > e200 and e21 > e50 and v > vm10 and rsi > 50
    except Exception:
        return False

def death_cross(df, ind):
    try:
        i    = ind.iloc[-1];  prev = ind.iloc[-2]
        e50  = _fv(i, 'ema_50');  e200 = _fv(i, 'ema_200');  e21 = _fv(i, 'ema_21')
        pe50 = _fv(prev, 'ema_50'); pe200 = _fv(prev, 'ema_200')
        rsi  = _fv(i, 'rsi');  v = df['volume'].iloc[-1]; vm10 = _fv(i, 'vwma_10')
        if any(np.isnan(x) for x in [e50, e200, e21, pe50, pe200, rsi, vm10]):
            return False
        return pe50 >= pe200 and e50 < e200 and e21 < e50 and v > vm10 and rsi < 50
    except Exception:
        return False

def stoch_bullish_crossover(df, ind):
    try:
        i    = ind.iloc[-1];  prev = ind.iloc[-2]
        sk   = _fv(i, 'stoch_k');  sd = _fv(i, 'stoch_d')
        psk  = _fv(prev, 'stoch_k'); psd = _fv(prev, 'stoch_d')
        v    = df['volume'].iloc[-1]; vm5 = _fv(i, 'vwma_5')
        if any(np.isnan(x) for x in [sk, sd, psk, psd, vm5]):
            return False
        return psk <= psd and sk > sd and sk < 30 and v > vm5
    except Exception:
        return False

def stoch_bearish_crossover(df, ind):
    try:
        i    = ind.iloc[-1];  prev = ind.iloc[-2]
        sk   = _fv(i, 'stoch_k');  sd = _fv(i, 'stoch_d')
        psk  = _fv(prev, 'stoch_k'); psd = _fv(prev, 'stoch_d')
        v    = df['volume'].iloc[-1]; vm5 = _fv(i, 'vwma_5')
        if any(np.isnan(x) for x in [sk, sd, psk, psd, vm5]):
            return False
        return psk >= psd and sk < sd and sk > 70 and v > vm5
    except Exception:
        return False

def adx_strong_trend_buy(df, ind):
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
    try:
        i   = ind.iloc[-1]
        adx = _fv(i, 'adx'); pdi = _fv(i, 'plus_di'); mdi = _fv(i, 'minus_di')
        e8  = _fv(i, 'ema_8'); e21 = _fv(i, 'ema_21')
        if any(np.isnan(x) for x in [adx, pdi, mdi, e8, e21]):
            return False
        return adx > 28 and mdi > pdi and mdi > 22 and e8 < e21
    except Exception:
        return False

def morning_star(df, ind):
    try:
        if len(df) < 4:
            return False
        c1 = df['close'].iloc[-3]; o1 = df['open'].iloc[-3]
        c2 = df['close'].iloc[-2]; o2 = df['open'].iloc[-2]
        h2 = df['high'].iloc[-2];  l2 = df['low'].iloc[-2]
        c3 = df['close'].iloc[-1]; o3 = df['open'].iloc[-1]
        v1 = df['volume'].iloc[-3]; v3 = df['volume'].iloc[-1]
        body1 = o1 - c1; body2 = abs(c2 - o2); range2 = h2 - l2 + 1e-9
        return (body1 > 0 and
                body2 / range2 < 0.35 and
                c3 > o3 and
                c3 > (o1 + c1) / 2 and
                v3 > v1 * 1.3)
    except Exception:
        return False

def evening_star(df, ind):
    try:
        if len(df) < 4:
            return False
        c1 = df['close'].iloc[-3]; o1 = df['open'].iloc[-3]
        c2 = df['close'].iloc[-2]; o2 = df['open'].iloc[-2]
        h2 = df['high'].iloc[-2];  l2 = df['low'].iloc[-2]
        c3 = df['close'].iloc[-1]; o3 = df['open'].iloc[-1]
        v1 = df['volume'].iloc[-3]; v3 = df['volume'].iloc[-1]
        body1 = c1 - o1; body2 = abs(c2 - o2); range2 = h2 - l2 + 1e-9
        return (body1 > 0 and
                body2 / range2 < 0.35 and
                c3 < o3 and
                c3 < (o1 + c1) / 2 and
                v3 > v1 * 1.3)
    except Exception:
        return False

# ========== ADVANCED STRATEGIES ==========
def bull_flag_breakout(df, ind):
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

def three_white_soldiers(df, ind):
    try:
        if len(df) < 5:
            return False
        rows = [(df['close'].iloc[j], df['open'].iloc[j],
                 df['high'].iloc[j], df['low'].iloc[j]) for j in [-3, -2, -1]]
        for c, o, h, l in rows:
            if c <= o: return False
            body = c - o; rng = h - l + 1e-9
            if body / rng < 0.5: return False
        c1, o1, *_ = rows[0]; c2, o2, *_ = rows[1]; c3, o3, *_ = rows[2]
        if not (c2 > c1 and c3 > c2): return False
        if not (o1 < o2 < c1 and o2 < o3 < c2): return False
        avg_vol = float(df['volume'].iloc[-5:-1].mean())
        return df['volume'].iloc[-1] > avg_vol
    except Exception:
        return False

def three_black_crows(df, ind):
    try:
        if len(df) < 5:
            return False
        rows = [(df['close'].iloc[j], df['open'].iloc[j],
                 df['high'].iloc[j], df['low'].iloc[j]) for j in [-3, -2, -1]]
        for c, o, h, l in rows:
            if c >= o: return False
            body = o - c; rng = h - l + 1e-9
            if body / rng < 0.5: return False
        c1, o1, *_ = rows[0]; c2, o2, *_ = rows[1]; c3, o3, *_ = rows[2]
        if not (c2 < c1 and c3 < c2): return False
        if not (o1 > o2 > c1 and o2 > o3 > c2): return False
        avg_vol = float(df['volume'].iloc[-5:-1].mean())
        return df['volume'].iloc[-1] > avg_vol
    except Exception:
        return False

def piercing_pattern(df, ind):
    try:
        if len(df) < 3:
            return False
        c1 = df['close'].iloc[-2]; o1 = df['open'].iloc[-2]; l1 = df['low'].iloc[-2]
        c2 = df['close'].iloc[-1]; o2 = df['open'].iloc[-1]
        body1 = o1 - c1
        if body1 <= 0: return False
        midpoint = (o1 + c1) / 2
        return (c2 > o2 and o2 < l1 and c2 > midpoint and c2 < o1)
    except Exception:
        return False

def dark_cloud_cover(df, ind):
    try:
        if len(df) < 3:
            return False
        c1 = df['close'].iloc[-2]; o1 = df['open'].iloc[-2]; h1 = df['high'].iloc[-2]
        c2 = df['close'].iloc[-1]; o2 = df['open'].iloc[-1]
        body1 = c1 - o1
        if body1 <= 0: return False
        midpoint = (o1 + c1) / 2
        return (c2 < o2 and o2 > h1 and c2 < midpoint and c2 > o1)
    except Exception:
        return False

def hammer_reversal(df, ind):
    try:
        if len(df) < 2:
            return False
        c = df['close'].iloc[-1]; o = df['open'].iloc[-1]
        h = df['high'].iloc[-1];  l = df['low'].iloc[-1]
        body         = abs(c - o)
        lower_shadow = min(c, o) - l
        upper_shadow = h - max(c, o)
        rng          = h - l + 1e-9
        if body <= 0 or lower_shadow <= 0: return False
        rsi = _fv(ind.iloc[-1], 'rsi')
        return (lower_shadow > body * 2.0 and
                upper_shadow < body * 0.4 and
                body / rng > 0.1 and
                c >= o and
                (rsi < 42 if not np.isnan(rsi) else False))
    except Exception:
        return False

def shooting_star_reversal(df, ind):
    try:
        if len(df) < 2:
            return False
        c = df['close'].iloc[-1]; o = df['open'].iloc[-1]
        h = df['high'].iloc[-1];  l = df['low'].iloc[-1]
        body         = abs(c - o)
        upper_shadow = h - max(c, o)
        lower_shadow = min(c, o) - l
        rng          = h - l + 1e-9
        if body <= 0 or upper_shadow <= 0: return False
        rsi = _fv(ind.iloc[-1], 'rsi')
        return (upper_shadow > body * 2.0 and
                lower_shadow < body * 0.4 and
                body / rng > 0.1 and
                c <= o and
                (rsi > 58 if not np.isnan(rsi) else False))
    except Exception:
        return False

def institutional_buying(df, ind):
    try:
        if len(df) < 12:
            return False
        closes  = df['close'].iloc[-5:].values
        volumes = df['volume'].iloc[-5:].values
        avg_vol = float(df['volume'].iloc[-10:-5].mean())
        if np.isnan(avg_vol) or avg_vol <= 0:
            return False
        consec_up  = all(closes[j] > closes[j-1] for j in range(1, 5))
        above_avg  = all(volumes[j] > avg_vol    for j in range(1, 5))
        vol_rising = volumes[-1] > volumes[-2]
        adx = _fv(ind.iloc[-1], 'adx')
        return (consec_up and above_avg and vol_rising and
                (adx > 22 if not np.isnan(adx) else False))
    except Exception:
        return False

def institutional_selling(df, ind):
    try:
        if len(df) < 12:
            return False
        closes  = df['close'].iloc[-5:].values
        volumes = df['volume'].iloc[-5:].values
        avg_vol = float(df['volume'].iloc[-10:-5].mean())
        if np.isnan(avg_vol) or avg_vol <= 0:
            return False
        consec_dn  = all(closes[j] < closes[j-1] for j in range(1, 5))
        above_avg  = all(volumes[j] > avg_vol    for j in range(1, 5))
        vol_rising = volumes[-1] > volumes[-2]
        adx = _fv(ind.iloc[-1], 'adx')
        return (consec_dn and above_avg and vol_rising and
                (adx > 22 if not np.isnan(adx) else False))
    except Exception:
        return False

def ema_cluster_buy(df, ind):
    try:
        i   = ind.iloc[-1]
        e8  = _fv(i,'ema_8');  e13 = _fv(i,'ema_13'); e21 = _fv(i,'ema_21')
        e34 = _fv(i,'ema_34'); e50 = _fv(i,'ema_50'); e100= _fv(i,'ema_100')
        e200= _fv(i,'ema_200'); vm10 = _fv(i,'vwma_10')
        c   = df['close'].iloc[-1]; c_prev = df['close'].iloc[-2]
        v   = df['volume'].iloc[-1]
        if any(np.isnan(x) for x in [e8,e13,e21,e34,e50,e100,e200,vm10]):
            return False
        stack = (e8 > e13 and e13 > e21 and e21 > e34 and
                 e34 > e50 and e50 > e100 and e100 > e200)
        return (stack and c > e8 and c > c_prev * 1.005 and v > vm10)
    except Exception:
        return False

def ema_cluster_sell(df, ind):
    try:
        i   = ind.iloc[-1]
        e8  = _fv(i,'ema_8');  e13 = _fv(i,'ema_13'); e21 = _fv(i,'ema_21')
        e34 = _fv(i,'ema_34'); e50 = _fv(i,'ema_50'); e100= _fv(i,'ema_100')
        e200= _fv(i,'ema_200'); vm10 = _fv(i,'vwma_10')
        c   = df['close'].iloc[-1]; c_prev = df['close'].iloc[-2]
        v   = df['volume'].iloc[-1]
        if any(np.isnan(x) for x in [e8,e13,e21,e34,e50,e100,e200,vm10]):
            return False
        stack = (e8 < e13 and e13 < e21 and e21 < e34 and
                 e34 < e50 and e50 < e100 and e100 < e200)
        return (stack and c < e8 and c < c_prev * 0.995 and v > vm10)
    except Exception:
        return False

def supertrend_breakout(df, ind):
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

def ichimoku_cloud_breakout(df, ind):
    try:
        if len(df) < 30:
            return False
        i      = ind.iloc[-1]
        spanA  = _fv(i, 'ichi_spanA');  spanB = _fv(i, 'ichi_spanB')
        tenkan = _fv(i, 'ichi_tenkan'); kijun = _fv(i, 'ichi_kijun')
        if any(np.isnan(x) for x in [spanA, spanB, tenkan, kijun]):
            return False
        c       = df['close'].iloc[-1]
        v       = df['volume'].iloc[-1]
        avg_vol = float(df['volume'].iloc[-7:-1].mean())
        cloud_top = max(spanA, spanB); cloud_bot = min(spanA, spanB)
        vol_ok    = v > avg_vol * 1.4
        bull = c > cloud_top and tenkan > kijun and vol_ok
        bear = c < cloud_bot  and tenkan < kijun and vol_ok
        return bull or bear
    except Exception:
        return False

def support_resistance_bounce(df, ind):
    try:
        if len(df) < 22:
            return False
        highs      = df['high'].iloc[-21:-1]
        lows       = df['low'].iloc[-21:-1]
        resistance = float(highs.quantile(0.80))
        support    = float(lows.quantile(0.20))
        c          = df['close'].iloc[-1]
        c_prev     = df['close'].iloc[-2]
        v          = df['volume'].iloc[-1]
        avg_vol    = float(df['volume'].iloc[-7:-1].mean())
        vol_ok     = v > avg_vol
        bounce  = c_prev <= support * 1.015 and c > support * 1.015 and c > c_prev and vol_ok
        reject  = c_prev >= resistance * 0.985 and c < resistance * 0.985 and c < c_prev and vol_ok
        return bounce or reject
    except Exception:
        return False

# ========== NEW INTRADAY STRATEGIES ==========
def vwap_reclaim_buy(df, ind):
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

# ========== STRATEGY REGISTRY ==========
all_strategies = {
    'SELL_SETUP':              sell_setup_original,
    'BUY_SETUP':               buy_setup_original,
    'RSI_OVERSOLD_BOUNCE':     rsi_oversold_bounce,
    'RSI_OVERBOUGHT_REVERSAL': rsi_overbought_reversal,
    'MACD_BULLISH':            macd_bullish_crossover,
    'MACD_BEARISH':            macd_bearish_crossover,
    'BB_BREAKOUT_BUY':         bollinger_breakout_buy,
    'BB_BREAKOUT_SELL':        bollinger_breakout_sell,
    'VOLUME_BREAKOUT':         volume_price_breakout,
    'GOLDEN_CROSS':            golden_cross,
    'DEATH_CROSS':             death_cross,
    'STOCH_BULLISH':           stoch_bullish_crossover,
    'STOCH_BEARISH':           stoch_bearish_crossover,
    'ADX_TREND_BUY':           adx_strong_trend_buy,
    'ADX_TREND_SELL':          adx_strong_trend_sell,
    'MORNING_STAR':            morning_star,
    'EVENING_STAR':            evening_star,
    'BULL_FLAG':               bull_flag_breakout,
    'BEAR_FLAG':               bear_flag_breakout,
    'THREE_SOLDIERS':          three_white_soldiers,
    'THREE_CROWS':             three_black_crows,
    'PIERCING':                piercing_pattern,
    'DARK_CLOUD':              dark_cloud_cover,
    'HAMMER':                  hammer_reversal,
    'SHOOTING_STAR_REVERSAL':  shooting_star_reversal,
    'INSTITUTIONAL_BUY':       institutional_buying,
    'INSTITUTIONAL_SELL':      institutional_selling,
    'EMA_CLUSTER_BUY':         ema_cluster_buy,
    'EMA_CLUSTER_SELL':        ema_cluster_sell,
    'SUPERTREND':              supertrend_breakout,
    'ICHIMOKU':                ichimoku_cloud_breakout,
    'SR_BOUNCE':               support_resistance_bounce,
    'VWAP_RECLAIM_BUY':        vwap_reclaim_buy,
    'VWAP_REJECTION_SELL':     vwap_rejection_sell,
    'ORB_BREAKOUT_BUY':        opening_range_breakout,
    'ORB_BREAKDOWN_SELL':      orb_breakdown,
    'MACD_FAST_BULL':          macd_fast_bullish,
    'MACD_FAST_BEAR':          macd_fast_bearish,
    'BB_SQUEEZE_BUY':          bb_squeeze_breakout_buy,
    'BB_SQUEEZE_SELL':         bb_squeeze_breakout_sell,
}