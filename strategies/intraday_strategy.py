# smc_orb_strategy_v2.py — LOB-C v2: Liquidity-Order-Block Continuum
# Institutional-grade intraday SMC strategy for NSE equities.
#
# Full written spec (philosophy, rule ranking, WHY-each-rule-exists,
# backtesting checklist, parameter optimization, failure modes, etc.)
# lives in SMC_ORB_Strategy_Spec_v2.md — this file is the direct,
# unambiguous code implementation of that spec.
#
# What changed vs v1 (smc_orb_strategy.py):
#   - Market structure: explicit HH/HL/LH/LL labeling + BOS vs CHOCH
#     distinction (CHOCH = first break AGAINST the prevailing trend,
#     BOS = break WITH the prevailing trend / continuation).
#   - Liquidity: equal-highs/equal-lows pool detection added alongside
#     the existing swing-sweep-and-reclaim logic.
#   - SMC: breaker blocks (a failed/invalidated OB that flips polarity
#     and is retested from the other side) and mitigation-block state
#     (fresh vs already-touched OB) added on top of OB + FVG.
#   - Price action: compression/expansion regime via Bollinger-Band-width
#     percentile, used both as a filter (no fresh entries while still
#     compressed) and as context for the ATR-expansion check.
#   - Volume: OBV slope added alongside relative volume; anchored VWAP
#     (anchored to the most recent confirmed opposing swing) added
#     alongside session VWAP.
#   - Trend filters: EMA(20/50) alignment and a higher-timeframe (15m,
#     resampled from the same 5m feed — no external call, no look-ahead)
#     structure/EMA bias added alongside Supertrend.
#   - Momentum: RSI kept as a gate only (never a trigger). MACD is OFF
#     by default (USE_MACD_FILTER=False) because, per spec, it is
#     included only if it demonstrably improves precision in walk-forward
#     testing on the specific universe/timeframe in use — ship it as an
#     inert, well-documented optional filter, not a default-on rule.
#   - Market filters: opening-gap filter, minimum turnover (price*volume)
#     liquidity filter, and a pluggable news/corporate-action hook.
#   - Risk/trade management: position sizing formula, partial-exit level,
#     breakeven trigger, and a trailing-stop suggestion are computed and
#     exposed via diagnostics (this file still only emits the boolean
#     entry signal for the scanner's plug-in contract; execution-level
#     SL/Target/trailing remain the caller's responsibility unless it
#     reads strategy_diagnostics, exactly as in v1).
#
# Non-repainting guarantee (unchanged principle from v1, re-verified for
# every new function below): every quantity used at bar i depends only on
# bars <= i. Swing/fractal points additionally require SWING_RIGHT bars of
# confirmation before they may be referenced. The 15m MTF bias is built by
# resampling only the already-closed 5m bars up to and including bar i,
# so the "current" 15m bar it sees is itself only as fresh as bar i.

import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# Core parameters
# =============================================================================
TIMEFRAME = "5minute"
MTF_TIMEFRAME_MINUTES = 15        # higher-timeframe bias, resampled from the 5m feed

NO_ENTRY_BEFORE_TIME = "09:30"    # HH:MM — skip the noisy opening window
MONITOR_CUTOFF_TIME = "14:30"     # HH:MM — no NEW signal after this
TIME_EXIT_TIME = "14:45"          # HH:MM — force-flatten any open runner not yet closed
SQUAREOFF_TIME = "15:15"          # HH:MM — hard EOD exit, no exceptions

# MIN_BARS_REQUIRED must cover: ATR/ADX/RSI/EMA/BB/volume-avg lookbacks (50)
# + fractal swing confirmation lag (SWING_LEFT+SWING_RIGHT) + a small buffer.
# Do NOT inflate this to "cover the whole monitoring window" — the engine
# won't evaluate a symbol until this many bars exist, so an oversized value
# delays the day's first check past MONITOR_CUTOFF_TIME and the strategy
# could never fire.
MIN_BARS_REQUIRED = 55

SWING_LEFT = 2
SWING_RIGHT = 2

ATR_PERIOD = 14
ADX_PERIOD = 14
RSI_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
BB_PERIOD = 20
BB_STD = 2.0

ADX_MIN = 20.0
RSI_BULL_MIN = 50.0
RSI_BEAR_MAX = 50.0

VOL_AVG_LOOKBACK = 20
REL_VOLUME_MIN = 1.5              # confirmation candle volume vs 20-bar avg
OBV_SLOPE_LOOKBACK = 10           # bars used to judge OBV trend direction

LIQUIDITY_SWEEP_LOOKBACK = 15     # bars searched for the swept swing level
EQUAL_LEVEL_LOOKBACK = 30         # bars searched for equal-highs/lows pools
EQUAL_LEVEL_TOLERANCE_ATR = 0.15  # max deviation (in ATRs) to call two levels "equal"
EQUAL_LEVEL_MIN_TOUCHES = 2       # minimum touches to call it a liquidity pool

OB_FVG_LOOKBACK = 15              # bars searched backwards for OB/FVG
BREAKER_LOOKBACK = 25             # bars searched backwards for a flipped/breaker OB

ATR_EXPANSION_LOOKBACK = 10       # bars used to judge "ATR expanding"
BB_WIDTH_PERCENTILE_LOOKBACK = 60 # bars used to build the BB-width percentile rank
BB_WIDTH_COMPRESSED_PCTL = 0.35   # below this percentile = "still compressed", skip
ATR_SL_MULT = 1.2                 # SL pad beyond the OB/breaker boundary, in ATRs
MIN_RR = 2.0                      # minimum acceptable reward:risk to fire

SUPER_TREND_PERIOD = 10
SUPER_TREND_MULT = 3.0

USE_MACD_FILTER = False           # OFF by default — see module docstring
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

GAP_ATR_MULT_MAX = 2.5            # skip the day's first hour if opening gap > this many ATRs
MIN_AVG_TURNOVER = 5_000_000      # ₹50 lakh avg (price*volume) over TURNOVER_LOOKBACK bars
TURNOVER_LOOKBACK = 20

RISK_PER_TRADE_PCT = 0.5          # % of capital risked per trade, for position sizing
PARTIAL_EXIT_R = 1.0              # take partial profit at +1R
BREAKEVEN_TRIGGER_R = 1.0         # move SL to breakeven once +1R is reached
TRAIL_METHOD = "structure"        # "structure" (trail behind swings) or "supertrend"


# =============================================================================
# Pluggable external hooks (no look-ahead: these must only use info that was
# actually available in real time as of the current bar's close)
# =============================================================================
def default_news_event_check(symbol, df, last_idx):
    """Placeholder. OHLCV data alone cannot tell you about scheduled
    earnings, board meetings, or breaking news. In production, wire this
    to your corporate-action / earnings-calendar / news-embargo feed and
    return False on any active embargo for `symbol`. Defaults to True
    (no embargo known) so the strategy is not silently crippled when no
    such feed is wired up — but this is a real gap, not a false all-clear;
    treat it as a mandatory integration task before going live."""
    return True


NEWS_EVENT_CHECK_FN = default_news_event_check


# =============================================================================
# Indicator helpers — all vectorized over the full session-anchored df, all
# causal (no shift(-1), no use of future bars anywhere in this file).
# =============================================================================

def _true_range(df):
    high = df['high'].astype(float)
    low = df['low'].astype(float)
    prev_close = df['close'].astype(float).shift(1)
    return pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def _atr(df, period=ATR_PERIOD):
    return _true_range(df).rolling(period).mean()


def _adx(df, period=ADX_PERIOD):
    high = df['high'].astype(float)
    low = df['low'].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    tr = _true_range(df)
    atr = tr.rolling(period).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).rolling(period).mean() / atr.replace(0, np.nan)
    minus_di = 100 * pd.Series(minus_dm, index=df.index).rolling(period).mean() / atr.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx = dx.rolling(period).mean()
    return adx


def _rsi(df, period=RSI_PERIOD):
    close = df['close'].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ema(series, period):
    return series.astype(float).ewm(span=period, adjust=False).mean()


def _macd(df, fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL):
    close = df['close'].astype(float)
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def _bollinger_bandwidth(df, period=BB_PERIOD, num_std=BB_STD):
    close = df['close'].astype(float)
    mid = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = mid + num_std * std
    lower = mid - num_std * std
    return (upper - lower) / mid.replace(0, np.nan)


def _bb_width_percentile(df, last_idx, lookback=BB_WIDTH_PERCENTILE_LOOKBACK):
    bw = _bollinger_bandwidth(df)
    if last_idx < lookback:
        return np.nan
    window = bw.iloc[last_idx - lookback + 1:last_idx + 1]
    cur = bw.iloc[last_idx]
    if pd.isna(cur) or window.dropna().empty:
        return np.nan
    return float((window <= cur).mean())


def _obv(df):
    close = df['close'].astype(float)
    vol = df['volume'].astype(float)
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * vol).cumsum()


def _obv_slope_confirms(df, last_idx, want_bullish, lookback=OBV_SLOPE_LOOKBACK):
    if last_idx < lookback:
        return False
    obv = _obv(df)
    now = obv.iloc[last_idx]
    past = obv.iloc[last_idx - lookback]
    if pd.isna(now) or pd.isna(past):
        return False
    return bool(now > past) if want_bullish else bool(now < past)


def _session_vwap(df, session_start):
    vwap = pd.Series(np.nan, index=df.index)
    sub = df.iloc[session_start:]
    typical = (sub['high'].astype(float) + sub['low'].astype(float) + sub['close'].astype(float)) / 3.0
    vol = sub['volume'].astype(float)
    cum_tpv = (typical * vol).cumsum()
    cum_vol = vol.cumsum()
    vwap.iloc[session_start:] = (cum_tpv / cum_vol.replace(0, np.nan)).values
    return vwap


def _anchored_vwap(df, anchor_idx, last_idx):
    """VWAP anchored to `anchor_idx` (e.g. the most recent confirmed
    opposing swing) up to `last_idx`. Purely backward-looking: only bars
    in [anchor_idx, last_idx] are used."""
    if anchor_idx is None or anchor_idx > last_idx:
        return np.nan
    sub = df.iloc[anchor_idx:last_idx + 1]
    typical = (sub['high'].astype(float) + sub['low'].astype(float) + sub['close'].astype(float)) / 3.0
    vol = sub['volume'].astype(float)
    tpv_sum = (typical * vol).sum()
    vol_sum = vol.sum()
    if vol_sum <= 0:
        return np.nan
    return float(tpv_sum / vol_sum)


def _supertrend(df, period=SUPER_TREND_PERIOD, mult=SUPER_TREND_MULT):
    """Standard, non-repainting Supertrend. direction[i]=1 bullish, -1 bearish,
    computed using only bars <= i."""
    atr = _atr(df, period)
    hl2 = (df['high'].astype(float) + df['low'].astype(float)) / 2.0
    close = df['close'].astype(float).values
    n = len(df)
    basic_upper = (hl2 + mult * atr).values
    basic_lower = (hl2 - mult * atr).values
    final_upper = np.full(n, np.nan)
    final_lower = np.full(n, np.nan)
    direction = np.ones(n, dtype=int)

    for i in range(n):
        if i == 0 or np.isnan(atr.iloc[i]):
            final_upper[i] = basic_upper[i]
            final_lower[i] = basic_lower[i]
            direction[i] = 1
            continue
        final_upper[i] = (basic_upper[i]
                           if (basic_upper[i] < final_upper[i - 1] or close[i - 1] > final_upper[i - 1])
                           else final_upper[i - 1])
        final_lower[i] = (basic_lower[i]
                           if (basic_lower[i] > final_lower[i - 1] or close[i - 1] < final_lower[i - 1])
                           else final_lower[i - 1])
        if not np.isnan(final_upper[i - 1]) and close[i] > final_upper[i - 1]:
            direction[i] = 1
        elif not np.isnan(final_lower[i - 1]) and close[i] < final_lower[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]

    return pd.Series(direction, index=df.index), pd.Series(final_lower, index=df.index), pd.Series(final_upper, index=df.index)


def _session_start_idx(df):
    """Index of the first bar of the current calendar day (today's 09:15
    candle). No 'date' column -> fail closed to 0 (whole df is one session)."""
    if 'date' not in df.columns or len(df) == 0:
        return 0
    try:
        dates = pd.to_datetime(df['date'])
        last_date = dates.iloc[-1].date()
        idxs = np.flatnonzero(dates.dt.date.values == last_date)
        return int(idxs[0]) if len(idxs) else 0
    except Exception:
        return 0


def _time_of(df, idx):
    ts = pd.to_datetime(df.iloc[idx]['date'])
    return ts.hour, ts.minute


def _within_trade_window(df, last_idx, start_hhmm=NO_ENTRY_BEFORE_TIME, end_hhmm=MONITOR_CUTOFF_TIME):
    """Fails open (True) if no 'date' column, matching the rest of this
    file's defensive fallbacks."""
    if 'date' not in df.columns:
        return True
    try:
        start_h, start_m = (int(x) for x in start_hhmm.split(':'))
        end_h, end_m = (int(x) for x in end_hhmm.split(':'))
        h, m = _time_of(df, last_idx)
        return (start_h, start_m) <= (h, m) <= (end_h, end_m)
    except Exception:
        return True


def _gap_filter_ok(df, session_start, last_idx, atr_series, max_atr_mult=GAP_ATR_MULT_MAX):
    """Reject signals whose session opened with a gap so large (vs ATR)
    that early moves are dominated by an unfilled overnight imbalance
    ("gap trap") rather than by intraday structure. Uses only the
    session's own open vs the prior session's close, both already known
    at session open — no look-ahead."""
    if session_start == 0 or session_start >= len(df):
        return True
    try:
        today_open = float(df.iloc[session_start]['open'])
        prior_close = float(df.iloc[session_start - 1]['close'])
        atr_ref = atr_series.iloc[session_start - 1] if session_start - 1 < len(atr_series) else np.nan
        if pd.isna(atr_ref) or atr_ref <= 0:
            return True
        gap = abs(today_open - prior_close)
        return bool(gap <= max_atr_mult * atr_ref)
    except Exception:
        return True


def _turnover_ok(df, last_idx, lookback=TURNOVER_LOOKBACK, min_turnover=MIN_AVG_TURNOVER):
    if last_idx < lookback:
        return False
    window = df.iloc[last_idx - lookback + 1:last_idx + 1]
    turnover = (window['close'].astype(float) * window['volume'].astype(float)).mean()
    if pd.isna(turnover):
        return False
    return bool(turnover >= min_turnover)


def _mtf_bias(df, last_idx):
    """Resample the CLOSED 5m bars up to and including last_idx into
    15m bars, then read EMA(20/50) alignment on that resampled series.
    Only uses df.iloc[:last_idx+1], so the newest 15m bar it can see is
    built strictly from already-closed 5m candles — no peeking into a
    still-forming higher-timeframe bar."""
    if 'date' not in df.columns:
        return 'neutral'
    sub = df.iloc[:last_idx + 1].copy()
    if len(sub) < 20:
        return 'neutral'
    try:
        sub['date'] = pd.to_datetime(sub['date'])
        sub = sub.set_index('date')
        agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
        resampled = sub.resample(f'{MTF_TIMEFRAME_MINUTES}min', label='left', closed='left').agg(agg).dropna()
        if len(resampled) < max(EMA_SLOW // 3, 15):
            return 'neutral'
        ema_fast = _ema(resampled['close'], max(5, EMA_FAST // 3))
        ema_slow = _ema(resampled['close'], max(10, EMA_SLOW // 3))
        f, s = ema_fast.iloc[-1], ema_slow.iloc[-1]
        close_now = resampled['close'].iloc[-1]
        if close_now > f > s:
            return 'bullish'
        if close_now < f < s:
            return 'bearish'
        return 'neutral'
    except Exception:
        return 'neutral'


def _confirmed_swings(df, session_start, last_idx, left=SWING_LEFT, right=SWING_RIGHT):
    """Confirmed fractal swing highs/lows within today's session, using only
    bars up to last_idx. A swing at position i is only usable once
    i+right <= last_idx — this guarantees no look-ahead."""
    highs = df['high'].astype(float).values
    lows = df['low'].astype(float).values
    last_confirmable = last_idx - right
    sh_idx, sh_val, sl_idx, sl_val = [], [], [], []
    lo = max(session_start, left)
    for i in range(lo, last_confirmable + 1):
        wh = highs[i - left:i + right + 1]
        wl = lows[i - left:i + right + 1]
        if len(wh) == left + right + 1 and highs[i] == wh.max() and int(np.argmax(wh)) == left:
            sh_idx.append(i)
            sh_val.append(highs[i])
        if len(wl) == left + right + 1 and lows[i] == wl.min() and int(np.argmin(wl)) == left:
            sl_idx.append(i)
            sl_val.append(lows[i])
    return sh_idx, sh_val, sl_idx, sl_val


def _structure_state(sh_val, sl_val):
    """'bullish' if last two confirmed swing highs AND lows are both rising
    (HH+HL), 'bearish' if both falling (LH+LL), else 'neutral'."""
    if len(sh_val) < 2 or len(sl_val) < 2:
        return 'neutral'
    hh = sh_val[-1] > sh_val[-2]
    hl = sl_val[-1] > sl_val[-2]
    lh = sh_val[-1] < sh_val[-2]
    ll = sl_val[-1] < sl_val[-2]
    if hh and hl:
        return 'bullish'
    if lh and ll:
        return 'bearish'
    return 'neutral'


def _structure_break_type(df, last_idx, sh_idx, sh_val, sl_idx, sl_val, prevailing, close_now):
    """Classify the most recent structural break as 'BOS' (break WITH the
    prevailing trend => continuation) or 'CHOCH' (break AGAINST the
    prevailing trend => the first sign of a potential reversal), relative
    to the last confirmed opposing swing level. Returns one of
    'BOS', 'CHOCH', or None if no break has actually happened yet."""
    if prevailing == 'bullish' and sh_val:
        if close_now > sh_val[-1]:
            return 'BOS'
    if prevailing == 'bearish' and sl_val:
        if close_now < sl_val[-1]:
            return 'BOS'
    if prevailing != 'bullish' and sh_val:
        if close_now > sh_val[-1]:
            return 'CHOCH'
    if prevailing != 'bearish' and sl_val:
        if close_now < sl_val[-1]:
            return 'CHOCH'
    return None


def _liquidity_swept_and_reclaimed(df, last_idx, level, bullish, lookback=LIQUIDITY_SWEEP_LOOKBACK):
    """Bullish case: some bar in [last_idx-lookback, last_idx] traded below
    `level` (the swing low was swept) AND the current close is back above
    `level` (reclaimed = failed breakdown / stop hunt). Bearish is the
    mirror."""
    start = max(0, last_idx - lookback)
    window = df.iloc[start:last_idx + 1]
    close_now = float(df.iloc[last_idx]['close'])
    if bullish:
        swept = (window['low'].astype(float) < level).any()
        return bool(swept and close_now > level)
    swept = (window['high'].astype(float) > level).any()
    return bool(swept and close_now < level)


def _equal_level_pool(values, indices, atr_now, tolerance_atr=EQUAL_LEVEL_TOLERANCE_ATR,
                       min_touches=EQUAL_LEVEL_MIN_TOUCHES, lookback_idx=None, last_idx=None,
                       lookback_bars=EQUAL_LEVEL_LOOKBACK):
    """Given a list of confirmed swing values (highs OR lows) and their bar
    indices, find the most recent cluster of >= min_touches levels within
    `tolerance_atr` ATRs of each other, restricted to the trailing
    `lookback_bars` window. Represents an "equal highs" / "equal lows"
    liquidity pool — a resting-stop magnet. Returns (pool_level,
    touch_count) or (None, 0)."""
    if atr_now is None or pd.isna(atr_now) or atr_now <= 0 or not values:
        return None, 0
    tol = tolerance_atr * atr_now
    pairs = list(zip(indices, values))
    if last_idx is not None:
        pairs = [(i, v) for i, v in pairs if i >= last_idx - lookback_bars]
    if len(pairs) < min_touches:
        return None, 0
    pairs.sort(key=lambda x: x[0])
    best_level, best_count = None, 0
    for i, (idx_i, val_i) in enumerate(pairs):
        cluster = [val_i]
        for idx_j, val_j in pairs[i + 1:]:
            if abs(val_j - val_i) <= tol:
                cluster.append(val_j)
        if len(cluster) >= min_touches and len(cluster) >= best_count:
            best_level = float(np.mean(cluster))
            best_count = len(cluster)
    return (best_level, best_count) if best_count >= min_touches else (None, 0)


def _find_bullish_ob_fvg(df, last_idx, lookback=OB_FVG_LOOKBACK):
    """Most recent bullish 3-candle Fair Value Gap (candle[i-2].high <
    candle[i].low) within lookback, plus the order block (last down-close
    candle immediately before the imbalance). Returns (ob_low, ob_high,
    fvg_low, fvg_high, ob_idx) or None."""
    start = max(2, last_idx - lookback)
    for i in range(last_idx, start - 1, -1):
        c0 = df.iloc[i - 2]
        ci = df.iloc[i]
        if float(ci['low']) > float(c0['high']):
            fvg_low, fvg_high = float(c0['high']), float(ci['low'])
            for j in range(i - 2, max(0, i - 2 - lookback), -1):
                cj = df.iloc[j]
                if float(cj['close']) < float(cj['open']):
                    return float(cj['low']), float(cj['high']), fvg_low, fvg_high, j
            break
    return None


def _find_bearish_ob_fvg(df, last_idx, lookback=OB_FVG_LOOKBACK):
    """Mirror of the bullish finder: bearish FVG is candle[i-2].low >
    candle[i].high; order block is the last up-close candle before it."""
    start = max(2, last_idx - lookback)
    for i in range(last_idx, start - 1, -1):
        c0 = df.iloc[i - 2]
        ci = df.iloc[i]
        if float(ci['high']) < float(c0['low']):
            fvg_low, fvg_high = float(ci['high']), float(c0['low'])
            for j in range(i - 2, max(0, i - 2 - lookback), -1):
                cj = df.iloc[j]
                if float(cj['close']) > float(cj['open']):
                    return float(cj['low']), float(cj['high']), fvg_low, fvg_high, j
            break
    return None


def _zone_invalidated(df, ob_idx, last_idx, zone_high, zone_low, bullish):
    """A bullish OB/FVG zone is invalidated if any candle strictly between
    its formation and now has fully CLOSED beyond the far side of the zone
    (i.e. the zone has already been consumed once, not merely wicked)."""
    if last_idx - 1 < ob_idx + 1:
        return False
    window = df.iloc[ob_idx + 1:last_idx]
    if bullish:
        return bool((window['close'].astype(float) < zone_low).any())
    return bool((window['close'].astype(float) > zone_high).any())


def _mitigation_state(df, ob_idx, last_idx, zone_low, zone_high):
    """'fresh' if price has never traded back into [zone_low, zone_high]
    since the zone formed (excluding the current, still-forming retest
    candle handled separately by the caller); 'mitigated' if it has been
    touched at least once already. Fresh zones are statistically the
    higher-probability reaction points — first-touch reactions are more
    reliable than n-th touch ones, which is why this is exposed as
    diagnostic context rather than a hard gate (a mitigated zone is not
    automatically rejected, just flagged as lower-confidence)."""
    if last_idx - 1 < ob_idx + 1:
        return 'fresh'
    window = df.iloc[ob_idx + 1:last_idx]
    touched = ((window['low'].astype(float) <= zone_high) & (window['high'].astype(float) >= zone_low)).any()
    return 'mitigated' if touched else 'fresh'


def _find_breaker_block(df, last_idx, want_bullish, lookback=BREAKER_LOOKBACK):
    """A breaker block is a former order block that failed (price closed
    through it, invalidating it as support/resistance in its original
    direction) and has since flipped polarity: the same zone now acts as
    resistance-turned-support (bullish breaker) or support-turned-
    resistance (bearish breaker) on the retest. This captures genuine
    institutional footprints — a level that trapped one side of the
    market and is now being defended by the other side.

    Bullish breaker: find a bearish OB/FVG zone that was invalidated by a
    later close ABOVE zone_high, and the current candle is retesting that
    same zone from above (low trades into it, closes back above).
    Bearish breaker is the mirror."""
    start = max(2, last_idx - lookback)
    if want_bullish:
        found = _find_bearish_ob_fvg(df, last_idx - 1, lookback) if last_idx - 1 >= 2 else None
    else:
        found = _find_bullish_ob_fvg(df, last_idx - 1, lookback) if last_idx - 1 >= 2 else None
    if found is None:
        return None
    ob_low, ob_high, fvg_low, fvg_high, ob_idx = found
    if ob_idx < start:
        return None
    zone_low = min(ob_low, fvg_low)
    zone_high = max(ob_high, fvg_high)
    window = df.iloc[ob_idx + 1:last_idx]
    if want_bullish:
        flipped = (window['close'].astype(float) > zone_high).any()
        if not flipped:
            return None
        cur = df.iloc[last_idx]
        retested = (float(cur['low']) <= zone_high and float(cur['close']) > zone_low
                    and float(cur['close']) > float(cur['open']))
        if retested:
            return zone_low, zone_high, ob_idx
    else:
        flipped = (window['close'].astype(float) < zone_low).any()
        if not flipped:
            return None
        cur = df.iloc[last_idx]
        retested = (float(cur['high']) >= zone_low and float(cur['close']) < zone_high
                    and float(cur['close']) < float(cur['open']))
        if retested:
            return zone_low, zone_high, ob_idx
    return None


def _atr_expanding(atr_series, last_idx, lookback=ATR_EXPANSION_LOOKBACK):
    if last_idx < lookback + 1:
        return False
    cur = atr_series.iloc[last_idx]
    past_mean = atr_series.iloc[last_idx - lookback:last_idx].mean()
    if pd.isna(cur) or pd.isna(past_mean) or past_mean <= 0:
        return False
    return bool(cur > past_mean)


def _volume_confirmed(df, last_idx, lookback=VOL_AVG_LOOKBACK, mult=REL_VOLUME_MIN):
    if last_idx < lookback:
        return False
    avg_vol = df['volume'].astype(float).iloc[last_idx - lookback:last_idx].mean()
    if pd.isna(avg_vol) or avg_vol <= 0:
        return False
    return float(df.iloc[last_idx]['volume']) >= mult * avg_vol


def position_size(capital, risk_pct, entry_price, stop_loss_price):
    """Shares to buy/sell so that a full stop-out loses exactly
    `risk_pct` % of `capital`. Pure risk-based sizing — decouples
    position size from conviction/emotion and keeps every trade's
    dollar (rupee) risk equal regardless of how wide or tight the
    structural stop happens to be."""
    risk_amount = capital * (risk_pct / 100.0)
    per_share_risk = abs(entry_price - stop_loss_price)
    if per_share_risk <= 0:
        return 0
    return int(risk_amount // per_share_risk)


# =============================================================================
# Core signal logic
# =============================================================================

def _build_signal(df, want_bullish, capital=None):
    required_cols = ('open', 'high', 'low', 'close', 'volume')
    if not all(c in df.columns for c in required_cols) or len(df) < MIN_BARS_REQUIRED:
        return None

    last_idx = len(df) - 1

    # --- Market / session filters (cheapest checks first) -------------------
    if not _within_trade_window(df, last_idx):
        return None

    session_start = _session_start_idx(df)

    if not _turnover_ok(df, last_idx):
        return None  # avoid illiquid stocks — wide slippage, unreliable fills

    atr = _atr(df)
    if not _gap_filter_ok(df, session_start, last_idx, atr):
        return None  # avoid gap traps

    symbol = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
    if not NEWS_EVENT_CHECK_FN(symbol, df, last_idx):
        return None  # avoid trading through scheduled news/events

    adx = _adx(df)
    if pd.isna(adx.iloc[last_idx]) or adx.iloc[last_idx] < ADX_MIN:
        return None  # avoid sideways/no-trend markets

    if not _atr_expanding(atr, last_idx):
        return None

    bbw_pctl = _bb_width_percentile(df, last_idx)
    if pd.isna(bbw_pctl) or bbw_pctl < BB_WIDTH_COMPRESSED_PCTL:
        return None  # still compressed / choppy — wait for genuine expansion

    # --- Market structure -----------------------------------------------
    sh_idx, sh_val, sl_idx, sl_val = _confirmed_swings(df, session_start, last_idx)
    structure = _structure_state(sh_val, sl_val)
    if want_bullish and structure != 'bullish':
        return None
    if (not want_bullish) and structure != 'bearish':
        return None

    close_now = float(df.iloc[last_idx]['close'])
    break_type = _structure_break_type(df, last_idx, sh_idx, sh_val, sl_idx, sl_val, structure, close_now)

    # --- Trend filters: EMA stack + higher-timeframe bias + Supertrend ------
    ema_fast = _ema(df['close'], EMA_FAST)
    ema_slow = _ema(df['close'], EMA_SLOW)
    if want_bullish and not (close_now > ema_fast.iloc[last_idx] > ema_slow.iloc[last_idx]):
        return None
    if (not want_bullish) and not (close_now < ema_fast.iloc[last_idx] < ema_slow.iloc[last_idx]):
        return None

    mtf = _mtf_bias(df, last_idx)
    if want_bullish and mtf == 'bearish':
        return None  # allow 'neutral' (MTF hasn't confirmed yet) but never fight it
    if (not want_bullish) and mtf == 'bullish':
        return None

    st_dir, st_lower, st_upper = _supertrend(df)
    if want_bullish and st_dir.iloc[last_idx] != 1:
        return None
    if (not want_bullish) and st_dir.iloc[last_idx] != -1:
        return None

    # --- Liquidity: sweep-and-reclaim of the last opposing swing ------------
    if not sl_val or not sh_val:
        return None

    if want_bullish:
        swept_level = sl_val[-1]
        prior_high_level = sh_val[-1]
        if not _liquidity_swept_and_reclaimed(df, last_idx, swept_level, bullish=True):
            return None
        if close_now <= prior_high_level:
            return None  # CHOCH/BOS not confirmed yet
    else:
        swept_level = sh_val[-1]
        prior_low_level = sl_val[-1]
        if not _liquidity_swept_and_reclaimed(df, last_idx, swept_level, bullish=False):
            return None
        if close_now >= prior_low_level:
            return None

    # Equal-highs/equal-lows liquidity pool context (diagnostic + used as the
    # RR target's liquidity destination when present — see target logic below).
    pool_level, pool_touches = _equal_level_pool(
        sh_val if not want_bullish else sl_val,
        sh_idx if not want_bullish else sl_idx,
        atr.iloc[last_idx], last_idx=last_idx,
    )
    opp_pool_level, opp_pool_touches = _equal_level_pool(
        sh_val if want_bullish else sl_val,
        sh_idx if want_bullish else sl_idx,
        atr.iloc[last_idx], last_idx=last_idx,
    )

    # --- SMC zone: order block + FVG (or breaker block if the plain OB is
    # unavailable / already fully consumed) ---------------------------------
    is_breaker = False
    if want_bullish:
        found = _find_bullish_ob_fvg(df, last_idx)
        zone_source = 'ob_fvg'
        if found is None:
            breaker = _find_breaker_block(df, last_idx, want_bullish=True)
            if breaker is None:
                return None
            zone_low, zone_high, ob_idx = breaker
            is_breaker = True
        else:
            ob_low, ob_high, fvg_low, fvg_high, ob_idx = found
            zone_low, zone_high = ob_low, fvg_high  # full mitigation zone: OB low .. FVG high
            if _zone_invalidated(df, ob_idx, last_idx, zone_high, zone_low, bullish=True):
                breaker = _find_breaker_block(df, last_idx, want_bullish=True)
                if breaker is None:
                    return None
                zone_low, zone_high, ob_idx = breaker
                is_breaker = True
        cur = df.iloc[last_idx]
        cur_low, cur_close, cur_open = float(cur['low']), float(cur['close']), float(cur['open'])
        retested = cur_low <= zone_high and cur_close > zone_low and cur_close > cur_open
        if not retested:
            return None
    else:
        found = _find_bearish_ob_fvg(df, last_idx)
        if found is None:
            breaker = _find_breaker_block(df, last_idx, want_bullish=False)
            if breaker is None:
                return None
            zone_low, zone_high, ob_idx = breaker
            is_breaker = True
        else:
            ob_low, ob_high, fvg_low, fvg_high, ob_idx = found
            zone_low, zone_high = fvg_low, ob_high
            if _zone_invalidated(df, ob_idx, last_idx, zone_high, zone_low, bullish=False):
                breaker = _find_breaker_block(df, last_idx, want_bullish=False)
                if breaker is None:
                    return None
                zone_low, zone_high, ob_idx = breaker
                is_breaker = True
        cur = df.iloc[last_idx]
        cur_high, cur_close, cur_open = float(cur['high']), float(cur['close']), float(cur['open'])
        retested = cur_high >= zone_low and cur_close < zone_high and cur_close < cur_open
        if not retested:
            return None

    mitigation = _mitigation_state(df, ob_idx, last_idx, zone_low, zone_high)

    # --- Volume confirmation: relative volume + OBV trend -------------------
    if not _volume_confirmed(df, last_idx):
        return None
    if not _obv_slope_confirms(df, last_idx, want_bullish):
        return None

    # --- VWAP: session VWAP + anchored VWAP (anchored to the swept swing) ---
    vwap = _session_vwap(df, session_start)
    vwap_now = vwap.iloc[last_idx]
    if pd.isna(vwap_now):
        return None
    if want_bullish and close_now <= vwap_now:
        return None
    if (not want_bullish) and close_now >= vwap_now:
        return None

    anchor_idx = sl_idx[-1] if want_bullish and sl_idx else (sh_idx[-1] if sh_idx else None)
    avwap_now = _anchored_vwap(df, anchor_idx, last_idx)
    if not pd.isna(avwap_now):
        if want_bullish and close_now <= avwap_now:
            return None
        if (not want_bullish) and close_now >= avwap_now:
            return None

    # --- Momentum gates: RSI always; MACD only if explicitly enabled --------
    rsi = _rsi(df).iloc[last_idx]
    if pd.isna(rsi):
        return None
    if want_bullish and rsi < RSI_BULL_MIN:
        return None
    if (not want_bullish) and rsi > RSI_BEAR_MAX:
        return None

    if USE_MACD_FILTER:
        _, _, hist = _macd(df)
        h_now, h_prev = hist.iloc[last_idx], hist.iloc[last_idx - 1]
        if pd.isna(h_now) or pd.isna(h_prev):
            return None
        if want_bullish and not (h_now > h_prev):
            return None
        if (not want_bullish) and not (h_now < h_prev):
            return None

    # --- Risk: structure + ATR based stop, liquidity-pool-aware target ------
    entry_price = close_now
    atr_now = atr.iloc[last_idx]
    if pd.isna(atr_now) or atr_now <= 0:
        return None

    if want_bullish:
        structure_stop = zone_low - ATR_SL_MULT * atr_now
        stop_loss = structure_stop
        risk = entry_price - stop_loss
        # Prefer the next unmitigated opposing liquidity pool as the target;
        # fall back to the last confirmed opposing swing; fall back to 2R.
        if opp_pool_level and opp_pool_level > entry_price:
            target = opp_pool_level
        elif sh_val and sh_val[-1] > entry_price:
            target = sh_val[-1]
        else:
            target = entry_price + MIN_RR * risk
        reward = target - entry_price
    else:
        structure_stop = zone_high + ATR_SL_MULT * atr_now
        stop_loss = structure_stop
        risk = stop_loss - entry_price
        if opp_pool_level and opp_pool_level < entry_price:
            target = opp_pool_level
        elif sl_val and sl_val[-1] < entry_price:
            target = sl_val[-1]
        else:
            target = entry_price - MIN_RR * risk
        reward = entry_price - target

    if risk <= 0:
        return None
    rr = reward / risk
    if rr < MIN_RR:
        return None

    partial_exit_price = entry_price + PARTIAL_EXIT_R * risk if want_bullish else entry_price - PARTIAL_EXIT_R * risk
    breakeven_trigger_price = entry_price + BREAKEVEN_TRIGGER_R * risk if want_bullish else entry_price - BREAKEVEN_TRIGGER_R * risk

    qty = None
    if capital is not None:
        qty = position_size(capital, RISK_PER_TRADE_PCT, entry_price, stop_loss)

    return {
        'entry': entry_price,
        'stop_loss': stop_loss,
        'target': target,
        'risk_reward': rr,
        'zone_low': zone_low,
        'zone_high': zone_high,
        'ob_idx': ob_idx,
        'is_breaker_block': is_breaker,
        'mitigation_state': mitigation,
        'break_type': break_type,
        'liquidity_pool_level': pool_level,
        'liquidity_pool_touches': pool_touches,
        'opposing_pool_level': opp_pool_level,
        'atr': atr_now,
        'adx': adx.iloc[last_idx],
        'rsi': rsi,
        'bb_width_percentile': bbw_pctl,
        'mtf_bias': mtf,
        'volume': float(df.iloc[last_idx]['volume']),
        'partial_exit_price': partial_exit_price,
        'partial_exit_r': PARTIAL_EXIT_R,
        'breakeven_trigger_price': breakeven_trigger_price,
        'trail_method': TRAIL_METHOD,
        'time_exit': TIME_EXIT_TIME,
        'squareoff_time': SQUAREOFF_TIME,
        'position_size': qty,
    }


# Tracks, per (symbol, direction, session), whether a signal has already
# fired from a given OB/breaker zone today — a simple in-process re-entry
# guard. Re-entry IS allowed from a genuinely new zone (different ob_idx),
# or after the original trade has closed AND a fresh liquidity sweep +
# structure confirmation sequence completes again from scratch — this
# dict only prevents duplicate signals off the *same* untouched zone.
_signaled_zones = {}


def _zone_already_signaled(symbol, direction, ob_idx, session_start):
    key = (symbol, direction, session_start)
    return _signaled_zones.get(key) == ob_idx


def _mark_zone_signaled(symbol, direction, ob_idx, session_start):
    key = (symbol, direction, session_start)
    _signaled_zones[key] = ob_idx


# =============================================================================
# Entry functions
# =============================================================================
def smc_buy(df, ind=None):
    try:
        required_cols = ('open', 'high', 'low', 'close', 'volume')
        if not all(c in df.columns for c in required_cols) or len(df) < MIN_BARS_REQUIRED:
            return False
        result = _build_signal(df, want_bullish=True)
        if result is None:
            return False
        session_start = _session_start_idx(df)
        symbol = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
        if _zone_already_signaled(symbol, 'BUY', result['ob_idx'], session_start):
            return False
        _mark_zone_signaled(symbol, 'BUY', result['ob_idx'], session_start)
        logger.info(
            f"SMC_BUY: {symbol} entry={result['entry']:.2f} SL={result['stop_loss']:.2f} "
            f"target={result['target']:.2f} RR={result['risk_reward']:.2f} "
            f"ADX={result['adx']:.1f} RSI={result['rsi']:.1f} ATR={result['atr']:.2f} "
            f"zone=({result['zone_low']:.2f},{result['zone_high']:.2f}) "
            f"breaker={result['is_breaker_block']} mitigation={result['mitigation_state']} "
            f"break_type={result['break_type']} mtf={result['mtf_bias']}"
        )
        return True
    except Exception as e:
        logger.error(f"SMC_BUY error: {e}")
        return False


def smc_sell(df, ind=None):
    try:
        required_cols = ('open', 'high', 'low', 'close', 'volume')
        if not all(c in df.columns for c in required_cols) or len(df) < MIN_BARS_REQUIRED:
            return False
        result = _build_signal(df, want_bullish=False)
        if result is None:
            return False
        session_start = _session_start_idx(df)
        symbol = df.iloc[-1].get('symbol', '?') if 'symbol' in df.columns else '?'
        if _zone_already_signaled(symbol, 'SELL', result['ob_idx'], session_start):
            return False
        _mark_zone_signaled(symbol, 'SELL', result['ob_idx'], session_start)
        logger.info(
            f"SMC_SELL: {symbol} entry={result['entry']:.2f} SL={result['stop_loss']:.2f} "
            f"target={result['target']:.2f} RR={result['risk_reward']:.2f} "
            f"ADX={result['adx']:.1f} RSI={result['rsi']:.1f} ATR={result['atr']:.2f} "
            f"zone=({result['zone_low']:.2f},{result['zone_high']:.2f}) "
            f"breaker={result['is_breaker_block']} mitigation={result['mitigation_state']} "
            f"break_type={result['break_type']} mtf={result['mtf_bias']}"
        )
        return True
    except Exception as e:
        logger.error(f"SMC_SELL error: {e}")
        return False


# --- Diagnostics: expose computed SL/Target/RR/trade-management fields for
# logging/monitoring only. These do NOT override the scanner's own flat
# target_pct/stoploss_pct execution settings unless the caller explicitly
# reads them.
def smc_buy_diagnostics(df, ind=None):
    try:
        return _build_signal(df, want_bullish=True)
    except Exception as e:
        logger.error(f"SMC_BUY diagnostics error: {e}")
        return None


def smc_sell_diagnostics(df, ind=None):
    try:
        return _build_signal(df, want_bullish=False)
    except Exception as e:
        logger.error(f"SMC_SELL diagnostics error: {e}")
        return None


def smc_exit_check(df, entry_result, direction, capital=None):
    """Stateful trade-management helper (NOT part of the scanner's
    boolean-signal contract — call this yourself from your position
    tracker once a trade from smc_buy/smc_sell is live). Given the
    original `entry_result` dict and the latest `df`, returns one of:
    'time_exit', 'squareoff', 'breakeven_hit', 'partial_target_hit',
    'trail_stop_hit', or None (still holding, no action yet). Pure
    function of already-closed bars — no look-ahead."""
    last_idx = len(df) - 1
    if 'date' in df.columns:
        h, m = _time_of(df, last_idx)
        eod_h, eod_m = (int(x) for x in SQUAREOFF_TIME.split(':'))
        te_h, te_m = (int(x) for x in TIME_EXIT_TIME.split(':'))
        if (h, m) >= (eod_h, eod_m):
            return 'squareoff'
        if (h, m) >= (te_h, te_m):
            return 'time_exit'
    close_now = float(df.iloc[last_idx]['close'])
    if direction == 'BUY':
        if close_now <= entry_result['stop_loss']:
            return 'trail_stop_hit'
        if close_now >= entry_result['partial_exit_price']:
            return 'partial_target_hit'
    else:
        if close_now >= entry_result['stop_loss']:
            return 'trail_stop_hit'
        if close_now <= entry_result['partial_exit_price']:
            return 'partial_target_hit'
    return None


# =============================================================================
# Metadata for scanner
# =============================================================================
strategy_diagnostics = {
    'SMC_BUY': smc_buy_diagnostics,
    'SMC_SELL': smc_sell_diagnostics,
}

strategy_exits = {}

all_strategies = {
    'SMC_BUY': smc_buy,
    'SMC_SELL': smc_sell,
}

strategy_meta = {
    'SMC_BUY': {'direction': 'BUY', 'category': 'smart_money_reversal', 'skip_quality_checks': True},
    'SMC_SELL': {'direction': 'SELL', 'category': 'smart_money_reversal', 'skip_quality_checks': True},
}