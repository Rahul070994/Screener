# ultimate_scanner.py  ─ v9.8  STRATEGY-AGNOSTIC SCORING
# ============================================================
# CHANGES in v9.8:
#   • Removed strategy-specific panel/gate scoring (v4_high_trust coupling)
#   • Simplified scoring to be strategy-agnostic
#   • All strategies now use native vote-based scoring
#   • Removed PANEL_GATE_STRATEGY constant
#   • Removed _panel_gates function and all references
#   • Removed panels_enabled() method
#   • Removed _null_gates() method
#   • Simplified _check_signal() to use pure vote-based scoring
#   • Simplified _score_stock() to use pure vote-based scoring
# ============================================================

from kiteconnect import KiteConnect
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, date
import webbrowser, os, time, json, threading, sys, importlib
from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from markupsafe import escape
import warnings, traceback, shutil, glob
from collections import deque
import logging
from logging.handlers import RotatingFileHandler
from threading import Lock
import hashlib
warnings.filterwarnings('ignore')

# ── Encryption ──────────────────────────────────────────────
from cryptography.fernet import Fernet
from dotenv import load_dotenv
from werkzeug.security import check_password_hash
load_dotenv()

def get_cipher():
    master_key = os.getenv("MASTER_SECRET_KEY")
    if not master_key:
        raise ValueError("MASTER_SECRET_KEY not set in .env file")
    try:
        return Fernet(master_key.encode())
    except Exception:
        import base64
        key = base64.urlsafe_b64encode(hashlib.sha256(master_key.encode()).digest())
        return Fernet(key)

def encrypt_secret(plain_text):
    return get_cipher().encrypt(plain_text.encode()).decode()

def decrypt_secret(cipher_text):
    return get_cipher().decrypt(cipher_text.encode()).decode()

# ── Strategy Registry ──────────────────────────────────────
import importlib.util
import os
import sys

# Force-load strategies.py from the file, not a folder with the same name
_strategies_file = os.path.join(os.path.dirname(__file__), 'strategies.py')
if os.path.isfile(_strategies_file):
    spec = importlib.util.spec_from_file_location("strategies_module", _strategies_file)
    strategies = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(strategies)
else:
    # Fallback to normal import (if file is missing)
    import strategies

AVAILABLE_STRATEGIES = strategies.STRATEGY_REGISTRY
# Flat {strategy_name: {'direction':..., 'high_trust':..., 'category':...}}
# sourced entirely from the strategy modules themselves (see each module's
# `strategy_meta`). The scanner never hardcodes a strategy name or infers
# its direction/trust/category from name keywords — it just looks it up
# here, so all strategy-specific knowledge stays in the strategies/ folder.
AVAILABLE_STRATEGY_META = strategies.STRATEGY_META
# Optional per-strategy diagnostics functions {strategy_name: fn(df, ind) -> dict}
# — see strategies.py's STRATEGY_DIAGNOSTICS docstring. Used only for
# displaying live "what is this strategy actually seeing right now" data
# in the Signal Log UI; never affects trigger/entry logic.
AVAILABLE_STRATEGY_DIAGNOSTICS = strategies.STRATEGY_DIAGNOSTICS
# Optional per-strategy early-exit functions {strategy_name: fn(df, ind, pos) -> bool}
# — see each strategy module's `strategy_exits` dict (e.g. intraday_strategy.py).
# A strategy opts in by registering a function here; the scanner has no
# built-in idea of what "the setup reversed" means for any given strategy
# (EMA flip, RSI exhaustion, whatever) — that logic lives entirely in the
# strategy module. getattr fallback keeps this working even for a
# strategies.py that predates this feature.
AVAILABLE_STRATEGY_EXITS = getattr(strategies, 'STRATEGY_EXITS', {})

# ── Modules (code-organization refactor — see modules/ folder) ─────
# modules/scanner.py (FullScanner — old Monitoring tab scan engine) and
# modules/market_movers.py (Market Movers computation) have been removed:
# the Monitoring tab and Market Movers panel were dropped from the UI, so
# nothing called their routes (/scanner/*, /market/movers) anymore.
from modules.index_universe import get_index_symbols
# HTML/LOGIN_HTML template strings were extracted out of this file for the
# same reason (line-count/maintainability) — see modules/templates.py.
# No behavior change: these are the identical strings, just relocated.
from modules.templates import HTML, LOGIN_HTML

# ── Logging ──────────────────────────────────────────────
def setup_logging():
    log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler = logging.handlers.RotatingFileHandler('alpha_scanner.log', maxBytes=10*1024*1024, backupCount=5)
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_formatter)
    console_handler.setLevel(logging.DEBUG)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logging.getLogger(__name__)

logger = setup_logging()

# ==================== RATE LIMITER ====================
class RateLimiter:
    def __init__(self, max_calls_per_second=1, max_calls_per_minute=55):
        self.max_per_second = max_calls_per_second
        self.max_per_minute = max_calls_per_minute
        self.calls_second   = deque(maxlen=max_calls_per_second)
        self.calls_minute   = deque(maxlen=max_calls_per_minute)
        self.lock           = Lock()
        self.total_calls    = 0
 
    def wait(self, caller_info=""):
        with self.lock:
            now = time.time()
            while self.calls_second and now - self.calls_second[0] > 1:
                self.calls_second.popleft()
            while self.calls_minute and now - self.calls_minute[0] > 60:
                self.calls_minute.popleft()
            if len(self.calls_second) >= self.max_per_second:
                sleep_time = 1.0 - (now - self.calls_second[0])
                if sleep_time > 0:
                    logger.debug(f"Throttling {caller_info} for {sleep_time:.3f}s")
                    time.sleep(sleep_time)
                    now = time.time()
            if len(self.calls_minute) >= self.max_per_minute:
                sleep_time = 60.0 - (now - self.calls_minute[0])
                if sleep_time > 0:
                    logger.warning(f"Rate limit (minute) hit — sleeping {sleep_time:.2f}s  [{caller_info}]")
                    time.sleep(sleep_time)
                    now = time.time()
            self.calls_second.append(time.time())
            self.calls_minute.append(time.time())
            self.total_calls += 1

_quote_limiter = RateLimiter(max_calls_per_second=1, max_calls_per_minute=55)
_hist_limiter  = RateLimiter(max_calls_per_second=3, max_calls_per_minute=170)
_other_limiter = RateLimiter(max_calls_per_second=9, max_calls_per_minute=550)

# ==================== JSON ENCODER ====================
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        return super().default(obj)

# ==================== CONFIGURATION ====================

# Anchor all relative app paths (data/, backups, etc.) to the directory this
# script lives in — NOT the process's current working directory.
# A bare relative path like "data/<user>" only resolves correctly when the
# app happens to be launched with CWD == the project folder (which is what
# happens when you run `python ultimate_scanner.py` by hand from inside it).
# If the same script is started a different way on a server — a scheduled
# task, a service wrapper (NSSM), a systemd unit with a different
# WorkingDirectory, pm2, supervisor, etc. — the CWD can be something else
# entirely, so "data/..." points at a folder that doesn't exist there, and
# every file op on it fails (WinError 2 on Windows / FileNotFoundError on
# Linux) even though the exact same code "worked" when run manually.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class Config:
    def __init__(self, user_id=None):
        self.user_id = user_id
        if user_id:
            self.data_dir = os.path.join(BASE_DIR, "data", user_id)
        else:
            self.data_dir = BASE_DIR
        os.makedirs(self.data_dir, exist_ok=True)
        self.TOKEN_FILE = os.path.join(self.data_dir, "access_token.txt")
        self.PAPER_FILE = os.path.join(self.data_dir, "paper_trading.json")
        self.SCAN_FILE = os.path.join(self.data_dir, "scan_results.json")
        self.USER_CONFIG_FILE = os.path.join(self.data_dir, "user_config.json")
        self.PAPER_BACKUP_DIR = os.path.join(self.data_dir, "backups")
        os.makedirs(self.PAPER_BACKUP_DIR, exist_ok=True)
        self.RATE_LIMIT = 0.1

# ==================== CONSTANTS ====================
HEAVYWEIGHTS = ['RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR',
                'ITC','KOTAKBANK','SBIN','BHARTIARTL','LT','WIPRO']

# ------------------------------------------------------------------
# NOTE ON WHERE THIS LIST COMES FROM:
# Kite Connect has no API for "which stocks are in index X" — it only
# returns quotes/history for a symbol you already give it. NSE itself is
# the only authoritative source for index membership. So the list below
# is now only a FALLBACK safety net (used only if NSE's site is
# unreachable AND no cache exists yet, e.g. very first run with no
# internet) — it is no longer the source of truth. The real, current
# list is fetched from NSE and cached to disk by get_index_symbols()
# right after this block. See modules/index_universe.py for details.
# ------------------------------------------------------------------
_NIFTY200_FALLBACK_SYMBOLS = [
    'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC','KOTAKBANK',
    'SBIN','BHARTIARTL','LT','WIPRO','HCLTECH','AXISBANK','ASIANPAINT','MARUTI',
    'SUNPHARMA','TITAN','BAJFINANCE','NESTLEIND','POWERGRID','NTPC','ULTRACEMCO',
    'TECHM','INDUSINDBK','TATAMOTORS','BAJAJFINSV','ONGC','JSWSTEEL','HINDALCO',
    'TATASTEEL','ADANIPORTS','COALINDIA','DRREDDY','DIVISLAB','CIPLA','EICHERMOT',
    'HEROMOTOCO','GRASIM','BRITANNIA','BPCL','SHREECEM','M&M','APOLLOHOSP',
    'BAJAJ-AUTO','ADANIENT','SBILIFE','HDFCLIFE','TATACONSUM','UPL',
    'HAVELLS','PIDILITIND','BERGEPAINT','TORNTPHARM','MUTHOOTFIN','CHOLAFIN',
    'PERSISTENT','LTIM','COFORGE','OFSS','MPHASIS','ZOMATO','DMART','INDIGO',
    'IRCTC','PNB','BANKBARODA','CANBK','UNIONBANK','IDFCFIRSTB','FEDERALBNK',
    'BANDHANBNK','ABCAPITAL','TRENT','NAUKRI','INFOEDGE','SBICARD','POLYCAB',
    'GODREJCP','DABUR','EMAMILTD','MARICO','COLPAL','VBL','JUBLFOOD',
    'PAGEIND','DLF','GODREJPROP','OBEROIRLTY','PRESTIGE',
    'SAIL','NMDC','HINDZINC','NATIONALUM','VEDL','JINDALSTEL',
    'MOTHERSON','BOSCHLTD','BHARATFORG','APOLLOTYRE','BALKRISIND','MRF',
    'AUROPHARMA','LUPIN','BIOCON','ALKEM','IPCALAB','LALPATHLAB',
    'MANAPPURAM','PNBHOUSING','MCDOWELL-N','UBL','RADICO',
    'GLOBALHEALTH','MAXHEALTH','FORTIS',
    'KPITTECH','ZEEL','SUNTV','TATAPOWER','ADANIGREEN','TORNTPOWER','CESC',
    'ASTRAL','AARTIIND','DEEPAKNITRITE','NAVINFLUOR','SRF','BALRAMCHIN',
    'BRIGADE','VOLTAS','WHIRLPOOL',
    'NYKAA','POLICYBZR','PAYTM','IXIGO','RATEGAIN',
    'GMRAIRPORT','CONCOR','TIINDIA','SCHAEFFLER','CUMMINSIND',
    'SUNDARMFIN','LTTS','CYIENT','TATAELXSI','DIXON',
    'KALYANKJIL','SENCO','RKFORGE','RBLBANK','YESBANK',
    'SJVN','NHPC','RECLTD','PFC','IRFC',
    'GLENMARK','TORNTPOWER','ATUL','FINEORG','ALKYLAMINE',
    'SYNGENE','METROPOLIS','THYROCARE','VIJAYA','MEDANTA',
    'SUPREMEIND','FINOLEX','JSWENERGY','CESC','ATGL',
    'ABFRL','MANYAVAR','BATAINDIA','RAJESHEXPO','VMART',
    'SUNDRMC','ENDURANCE','EXIDEIND','AMARAJABAT','GREENPANEL',
    'GREENPLY','CENTURYPLY','ORIENTELEC','SYRMA','KAYNES',
    'HONAUT','ABB','SIEMENS','BHEL','CGPOWER',
    'JINDALSAW','RATNAMANI','WELCORP','APL','GHCL',
]

# Real, current NIFTY 200 universe — fetched from NSE's official CSV and
# cached to disk (data/nifty200_constituents.json) for ~24h at a time so
# we're not re-hitting NSE on every process start. Falls back to the
# stale cache, then to _NIFTY200_FALLBACK_SYMBOLS above, if NSE can't be
# reached. See modules/index_universe.py.
NIFTY200_SYMBOLS = get_index_symbols(
    "NIFTY200",
    cache_dir=os.path.join(BASE_DIR, "data"),
    fallback_symbols=_NIFTY200_FALLBACK_SYMBOLS,
)
_seen = set()
NIFTY200_SYMBOLS = [s for s in NIFTY200_SYMBOLS if not (s in _seen or _seen.add(s))]

TRADE_SLOTS = [
    (9*60+15,  14*60+30),
]

def _in_trade_slot(mins: int) -> bool:
    return any(start <= mins <= end for start, end in TRADE_SLOTS)

def _slot_label(mins: int) -> str:
    if   9*60+15  <= mins <= 14*60+30:  return "SLOT-1 (9:15–14:30 Trading Window)"
    elif mins < 9*60+15:               return "PRE-MARKET"
    else:                              return "CLOSED"

SECTOR_MAP = {
    "IT": ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS", "COFORGE", "PERSISTENT", "OFSS"],
    "BANKING": ["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK", "FEDERALBNK", "BANDHANBNK", "PNB", "BANKBARODA"],
    "PHARMA": ["SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "LUPIN", "AUROPHARMA", "BIOCON", "ALKEM", "IPCALAB", "LALPATHLAB"],
    "AUTO": ["MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "EICHERMOT", "HEROMOTOCO", "APOLLOTYRE", "BALKRISIND", "MRF", "MOTHERSON"],
    "FMCG": ["HINDUNILVR", "ITC", "NESTLEIND", "BRITANNIA", "DABUR", "MARICO", "COLPAL", "GODREJCP", "EMAMILTD", "VBL"],
    "METALS": ["TATASTEEL", "HINDALCO", "JSWSTEEL", "SAIL", "NMDC", "VEDL", "NATIONALUM", "HINDZINC", "JINDALSTEL"],
    "OIL_GAS": ["RELIANCE", "ONGC", "BPCL", "IOC", "GAIL", "PETRONET", "GUJGAS", "MGL"],
    "POWER": ["NTPC", "POWERGRID", "TATAPOWER", "ADANIPOWER", "ADANIGREEN", "TORNTPOWER", "CESC", "SIEMENS"],
    "INFRA": ["LT", "DLF", "GODREJPROP", "OBEROIRLTY", "PRESTIGE", "BRIGADE", "IRB", "NBCC"],
    "TECH_MEDIA": ["TECHM", "INFY", "WIPRO", "HCLTECH", "TCS", "MPHASIS", "COFORGE", "PERSISTENT", "ZEEL", "SUNTV"],
}

# ==================== ZERODHA CHARGES ====================
def calc_zerodha_charges(buy_price, sell_price, qty, exchange='NSE'):
    try:
        tb = buy_price * qty
        ts = sell_price * qty
        tt = tb + ts
        
        bb = min(tb * 0.0003, 20.0)
        bs = min(ts * 0.0003, 20.0)
        total_brok = round(bb + bs, 2)
        
        stt = round(ts * 0.00025, 2)
        exch = round(tt * 0.0000297, 2)
        sebi = round(tt * 0.000001, 2)
        gst = round((total_brok + exch) * 0.18, 2)
        stmp = round(tb * 0.00003, 2)
        
        total_chg = round(total_brok + stt + exch + sebi + gst + stmp, 2)
        gross = round((sell_price - buy_price) * qty, 2)
        net = round(gross - total_chg, 2)
        
        return {
            'brokerage': total_brok,
            'stt': stt,
            'exchange_charge': exch,
            'sebi_charges': sebi,
            'gst': gst,
            'stamp_duty': stmp,
            'total_charges': total_chg,
            'gross_pnl': gross,
            'net_pnl': net,
            'breakeven_pts': round(total_chg / qty, 4) if qty > 0 else 0,
            'turnover': round(tt, 2),
        }
    except Exception as e:
        logger.error(f"Charge calculation error: {e}")
        return {
            'brokerage': 0, 'stt': 0, 'exchange_charge': 0, 'sebi_charges': 0,
            'gst': 0, 'stamp_duty': 0, 'total_charges': 0,
            'gross_pnl': 0, 'net_pnl': 0, 'breakeven_pts': 0, 'turnover': 0
        }

# ==================== GLOBAL INSTRUMENT CACHE ====================
_instrument_cache = None

def get_instrument_cache(kite):
    global _instrument_cache, SYMBOL_MAP
    if _instrument_cache is None:
        try:
            _other_limiter.wait("instruments")
            instruments = kite.instruments("NSE")
            stocks = []
            skip = ["NIFTY", "SENSEX", "BANKEX", "MIDCAP", "SMALLCAP"]
            for inst in instruments:
                if (inst["segment"] == "NSE"
                    and inst["instrument_type"] == "EQ"
                    and not any(x in inst["tradingsymbol"] for x in skip)):
                    stocks.append({
                        "token":  inst["instrument_token"],
                        "symbol": inst["tradingsymbol"],
                        "name":   inst.get("name", inst["tradingsymbol"]),
                    })
            _instrument_cache = stocks
            SYMBOL_MAP = {s['symbol']: s for s in stocks}
            logger.info(f"Instrument cache loaded: {len(stocks)} stocks")
        except Exception as e:
            logger.error(f"Failed to fetch instruments: {e}")
            _instrument_cache = []
    return _instrument_cache

# ==================== USER MANAGER ====================
class UserManager:
    _users = None
    _kites = {}
    _paper_engines = {}
    _sector_monitors = {}
    _backtest_engines = {}

    @classmethod
    def load_users(cls):
        if cls._users is None:
            with open("users.json", "r") as f:
                raw_data = json.load(f)
            decrypted = {}
            for user_id, data in raw_data.items():
                decrypted[user_id] = {
                    "name": data["name"],
                    "kite_api_key": decrypt_secret(data["kite_api_key"]),
                    "kite_api_secret": decrypt_secret(data["kite_api_secret"]),
                    "password_hash": data.get("password_hash", ""),
                }
            cls._users = decrypted
        return cls._users

    @classmethod
    def reload_users(cls):
        cls._users = None
        return cls.load_users()

    @classmethod
    def get_user_data(cls, user_id):
        users = cls.load_users()
        return users.get(user_id)

    @classmethod
    def get_kite(cls, user_id):
        if user_id not in cls._kites:
            user_data = cls.get_user_data(user_id)
            if not user_data:
                raise ValueError(f"User {user_id} not found")
            config = Config(user_id)
            if os.path.exists(config.TOKEN_FILE):
                with open(config.TOKEN_FILE, "r") as f:
                    access_token = f.read().strip()
                kite = KiteConnect(api_key=user_data["kite_api_key"])
                kite.set_access_token(access_token)
                cls._kites[user_id] = kite
            else:
                kite = KiteConnect(api_key=user_data["kite_api_key"])
                cls._kites[user_id] = kite
        return cls._kites[user_id]

    @classmethod
    def set_access_token(cls, user_id, access_token):
        user_data = cls.get_user_data(user_id)
        config = Config(user_id)
        with open(config.TOKEN_FILE, "w") as f:
            f.write(access_token)
        kite = KiteConnect(api_key=user_data["kite_api_key"])
        kite.set_access_token(access_token)
        cls._kites[user_id] = kite

    @classmethod
    def get_user_config(cls, user_id):
        config = Config(user_id)
        if os.path.exists(config.USER_CONFIG_FILE):
            with open(config.USER_CONFIG_FILE, "r") as f:
                return json.load(f)
        return {}

    @classmethod
    def save_user_config(cls, user_id, config_data):
        config = Config(user_id)
        with open(config.USER_CONFIG_FILE, "w") as f:
            json.dump(config_data, f, indent=2)

    @classmethod
    def get_user_strategy(cls, user_id):
        cfg = cls.get_user_config(user_id)
        strategy_name = cfg.get('strategy')
        if strategy_name and strategy_name in AVAILABLE_STRATEGIES:
            return strategy_name, AVAILABLE_STRATEGIES[strategy_name]
        if not AVAILABLE_STRATEGIES:
            return None, {}
        default = next(iter(AVAILABLE_STRATEGIES))
        return default, AVAILABLE_STRATEGIES[default]

    @classmethod
    def get_user_mode(cls, user_id):
        cfg = cls.get_user_config(user_id)
        mode = cfg.get('trading_mode', 'INTRADAY')
        return mode if mode in ('INTRADAY', 'DELIVERY') else 'INTRADAY'

    @classmethod
    def set_user_mode(cls, user_id, mode):
        if mode not in ('INTRADAY', 'DELIVERY'):
            raise ValueError(f"Invalid mode {mode}")
        cfg = cls.get_user_config(user_id)
        cfg['trading_mode'] = mode
        cls.save_user_config(user_id, cfg)

    @classmethod
    def set_user_strategy(cls, user_id, strategy_name):
        if strategy_name not in AVAILABLE_STRATEGIES:
            raise ValueError(f"Strategy {strategy_name} not found")
        cfg = cls.get_user_config(user_id)
        cfg['strategy'] = strategy_name
        cls.save_user_config(user_id, cfg)

    # Target/SL are configured per user, per trading mode (Intraday vs
    # CNC/Delivery) — used by live paper trading (PaperTradingEngine) and by
    # BacktestEngine, so both stop reading fixed class constants and instead
    # read whatever the user set in Settings -> Target & Stop Loss.
    DEFAULT_RISK_CONFIG = {
        'target_pct_intraday': 1.0,     # percent, i.e. 1.0 = 1%
        'stoploss_pct_intraday': 0.5,
        'target_pct_delivery': 3.0,     # CNC/swing trades want more room
        'stoploss_pct_delivery': 1.5,
        'min_hold_days_delivery': 1,    # calendar days before CNC auto-exit allowed
    }

    @classmethod
    def get_user_risk_config(cls, user_id):
        cfg = cls.get_user_config(user_id)
        rc = dict(cls.DEFAULT_RISK_CONFIG)
        rc.update(cfg.get('risk_config', {}) or {})
        return rc

    @classmethod
    def set_user_risk_config(cls, user_id, data):
        rc = cls.get_user_risk_config(user_id)
        for key in cls.DEFAULT_RISK_CONFIG:
            if key not in data or data[key] in (None, ''):
                continue
            if key == 'min_hold_days_delivery':
                try:
                    iv = int(float(data[key]))
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be a whole number")
                if iv < 0 or iv > 30:
                    raise ValueError(f"{key} must be between 0 and 30 (days)")
                rc[key] = iv
                continue
            try:
                v = float(data[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            if v <= 0 or v > 20:
                raise ValueError(f"{key} must be between 0 and 20 (percent)")
            rc[key] = v
        cfg = cls.get_user_config(user_id)
        cfg['risk_config'] = rc
        cls.save_user_config(user_id, cfg)
        return rc

    @classmethod
    def get_paper_engine(cls, user_id):
        if user_id not in cls._paper_engines:
            config = Config(user_id)
            strategy_name, strategies_dict = cls.get_user_strategy(user_id)
            trading_mode = cls.get_user_mode(user_id)
            risk_config = cls.get_user_risk_config(user_id)
            pe = PaperTradingEngine(config, strategies_dict, trading_mode=trading_mode,
                                     strategy_name=strategy_name, risk_config=risk_config)
            cls._paper_engines[user_id] = pe
            pe.start(cls.get_kite(user_id))
        return cls._paper_engines[user_id]

    @classmethod
    def get_sector_monitor(cls, user_id):
        if user_id not in cls._sector_monitors:
            pe = cls.get_paper_engine(user_id)
            sm = SectorMonitor(pe)
            cls._sector_monitors[user_id] = sm
            sm.start()
        return cls._sector_monitors[user_id]

    @classmethod
    def ensure_authenticated(cls, user_id):
        try:
            kite = cls.get_kite(user_id)
            _other_limiter.wait("profile")
            kite.profile()
            return True
        except Exception:
            return False

# ==================== NIFTY HELPERS ====================
def get_nifty_data(kite):
    try:
        _hist_limiter.wait("nifty data")
        data = kite.historical_data(
            256265,
            datetime.now() - timedelta(days=1),
            datetime.now(),
            "3minute",
        )
        if data and len(data) > 5:
            df  = pd.DataFrame(data)
            ind = Indicators.calculate_all(df)
            return {
                "close":      float(df["close"].iloc[-1]),
                "change":     float(
                    (df["close"].iloc[-1] - df["close"].iloc[-2])
                    / df["close"].iloc[-2] * 100
                ),
                "adx":        float(ind["adx"].iloc[-1]) if "adx" in ind.columns else 0,
                "volume":     int(df["volume"].iloc[-1]),
                "avg_volume": int(df["volume"].iloc[-20:].mean()),
            }
    except Exception as e:
        logger.error(f"NIFTY data error: {e}")
    return None

# ==================== INDICATORS ====================
class Indicators:
    @staticmethod
    def calculate_all(df):
        try:
            ind = {}
            ind['close'] = df['close']
            ind['open'] = df['open']
            ind['high'] = df['high']
            ind['low'] = df['low']
            ind['volume'] = df['volume']
            
            for p in [5, 8, 9, 10, 13, 20, 21, 34, 50, 100, 200]:
                ind[f'ema_{p}'] = df['close'].ewm(span=p, adjust=False).mean()
                ind[f'sma_{p}'] = df['close'].rolling(p).mean()
            
            def _rsi(s, n=14):
                d = s.diff()
                g = d.where(d > 0, 0).ewm(com=n-1, adjust=False).mean()
                l = (-d.where(d < 0, 0)).ewm(com=n-1, adjust=False).mean()
                return 100 - 100 / (1 + g / l.clip(lower=1e-10))
            
            ind['rsi'] = _rsi(df['close'], 14)
            ind['rsi7'] = _rsi(df['close'], 7)
            ind['rsi21'] = _rsi(df['close'], 21)
            
            e12 = df['close'].ewm(span=12, adjust=False).mean()
            e26 = df['close'].ewm(span=26, adjust=False).mean()
            ind['macd'] = e12 - e26
            ind['macd_signal'] = ind['macd'].ewm(span=9, adjust=False).mean()
            ind['macd_hist'] = ind['macd'] - ind['macd_signal']
            
            e5 = df['close'].ewm(span=5, adjust=False).mean()
            e13 = df['close'].ewm(span=13, adjust=False).mean()
            ind['macd_fast'] = e5 - e13
            ind['macd_fast_signal'] = ind['macd_fast'].ewm(span=3, adjust=False).mean()
            ind['macd_fast_hist'] = ind['macd_fast'] - ind['macd_fast_signal']
            
            s20 = df['close'].rolling(20).mean()
            d20 = df['close'].rolling(20).std()
            ind['bb_upper'] = s20 + d20 * 2
            ind['bb_middle'] = s20
            ind['bb_lower'] = s20 - d20 * 2
            ind['bb_width'] = (ind['bb_upper'] - ind['bb_lower']) / ind['bb_middle'].clip(lower=1e-10)
            ind['bb_position'] = (df['close'] - ind['bb_lower']) / (ind['bb_upper'] - ind['bb_lower']).clip(lower=1e-10)
            
            tr = pd.concat([
                df['high'] - df['low'],
                (df['high'] - df['close'].shift()).abs(),
                (df['low'] - df['close'].shift()).abs()
            ], axis=1).max(axis=1)
            ind['atr'] = tr.rolling(14).mean()
            ind['atr_percent'] = ind['atr'] / df['close'] * 100
            atr10 = tr.rolling(10).mean()
            
            ema20 = df['close'].ewm(span=20, adjust=False).mean()
            ind['kc_upper'] = ema20 + 1.5 * ind['atr']
            ind['kc_lower'] = ema20 - 1.5 * ind['atr']
            ind['squeeze'] = ((ind['bb_upper'] < ind['kc_upper']) & 
                              (ind['bb_lower'] > ind['kc_lower'])).astype(float)
            
            pdm = df['high'].diff().clip(lower=0)
            ndm = (-df['low'].diff()).clip(lower=0)
            tr14 = tr.rolling(14).mean().clip(lower=1e-10)
            pdi = 100 * (pdm.rolling(14).mean() / tr14)
            ndi = 100 * (ndm.rolling(14).mean() / tr14)
            dx = 100 * (pdi - ndi).abs() / (pdi + ndi).clip(lower=1e-10)
            ind['adx'] = dx.rolling(14).mean()
            ind['plus_di'] = pdi
            ind['minus_di'] = ndi
            
            lo14 = df['low'].rolling(14).min()
            hi14 = df['high'].rolling(14).max()
            ind['stoch_k'] = 100 * ((df['close'] - lo14) / (hi14 - lo14).clip(lower=1e-10))
            ind['stoch_d'] = ind['stoch_k'].rolling(3).mean()
            
            rsi14 = ind['rsi']
            rsi_lo = rsi14.rolling(14).min()
            rsi_hi = rsi14.rolling(14).max()
            ind['stochrsi_k'] = 100 * (rsi14 - rsi_lo) / (rsi_hi - rsi_lo).clip(lower=1e-10)
            ind['stochrsi_d'] = ind['stochrsi_k'].rolling(3).mean()
            
            tp = (df['high'] + df['low'] + df['close']) / 3
            mf = tp * df['volume']
            pmf = mf.where(tp > tp.shift(), 0).rolling(14).sum()
            nmf = mf.where(tp < tp.shift(), 0).rolling(14).sum()
            ind['mfi'] = 100 - (100 / (1 + pmf / nmf.clip(lower=1e-10)))
            
            ind['obv'] = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
            ind['obv_ma'] = ind['obv'].rolling(20).mean()
            ind['obv_slope'] = ind['obv'].diff(3) / (ind['obv'].shift(3).abs() + 1e-10) * 100
            
            pc = df['close'].pct_change().fillna(0)
            ind['vpt'] = (pc * df['volume']).fillna(0).cumsum()
            ind['vpt_ma'] = ind['vpt'].rolling(14).mean()
            
            mf_mult = ((df['close'] - df['low']) - (df['high'] - df['close'])) / (df['high'] - df['low']).clip(lower=1e-10)
            ind['cmf'] = (mf_mult * df['volume']).rolling(21).sum() / df['volume'].rolling(21).sum().clip(lower=1e-10)
            
            try:
                dt_col = pd.to_datetime(df['date'] if 'date' in df.columns else df.index)
                _dates = dt_col if hasattr(dt_col, 'dt') else pd.Series(dt_col.values, index=df.index)
                _day = _dates.dt.date if hasattr(_dates, 'dt') else _dates.apply(lambda x: x.date())
                _cum_tp_vol = (tp * df['volume']).groupby(_day).cumsum()
                _cum_vol = df['volume'].groupby(_day).cumsum().clip(lower=1e-10)
                ind['vwap'] = _cum_tp_vol / _cum_vol
            except Exception:
                ind['vwap'] = (tp * df['volume']).rolling(20).sum() / df['volume'].rolling(20).sum().clip(lower=1e-10)
            vwap_var = ((tp - ind['vwap']) ** 2 * df['volume']).rolling(20).sum() / df['volume'].rolling(20).sum().clip(lower=1e-10)
            vwap_std = np.sqrt(vwap_var)
            ind['vwap_upper1'] = ind['vwap'] + vwap_std
            ind['vwap_lower1'] = ind['vwap'] - vwap_std
            ind['vwap_upper2'] = ind['vwap'] + 2 * vwap_std
            ind['vwap_lower2'] = ind['vwap'] - 2 * vwap_std
            
            ind['willr'] = -100 * (hi14 - df['close']) / (hi14 - lo14).clip(lower=1e-10)
            
            tp_cci = (df['high'] + df['low'] + df['close']) / 3
            mad = tp_cci.rolling(20).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
            ind['cci'] = (tp_cci - tp_cci.rolling(20).mean()) / (0.015 * mad + 1e-10)
            
            hi9 = df['high'].rolling(9).max()
            lo9 = df['low'].rolling(9).min()
            hi26 = df['high'].rolling(26).max()
            lo26 = df['low'].rolling(26).min()
            hi52 = df['high'].rolling(52).max()
            lo52 = df['low'].rolling(52).min()
            
            ind['ichi_tenkan'] = (hi9 + lo9) / 2
            ind['ichi_kijun'] = (hi26 + lo26) / 2
            ind['ichi_spanA'] = ((ind['ichi_tenkan'] + ind['ichi_kijun']) / 2).shift(26)
            ind['ichi_spanB'] = ((hi52 + lo52) / 2).shift(26)
            ind['ichi_chikou'] = df['close'].shift(-26)
            
            hl_avg = (df['high'] + df['low']) / 2
            basic_upper = hl_avg + 3 * atr10
            basic_lower = hl_avg - 3 * atr10
            
            st_u = basic_upper.copy()
            st_l = basic_lower.copy()
            st = pd.Series(np.nan, index=df.index)
            st_d = pd.Series(1, index=df.index)
            
            for k in range(1, len(df)):
                st_u.iloc[k] = (basic_upper.iloc[k] if (basic_upper.iloc[k] < st_u.iloc[k-1] or 
                                                       df['close'].iloc[k-1] > st_u.iloc[k-1]) 
                                else st_u.iloc[k-1])
                st_l.iloc[k] = (basic_lower.iloc[k] if (basic_lower.iloc[k] > st_l.iloc[k-1] or 
                                                       df['close'].iloc[k-1] < st_l.iloc[k-1]) 
                                else st_l.iloc[k-1])
                if df['close'].iloc[k] > st_u.iloc[k]:
                    st_d.iloc[k] = 1
                elif df['close'].iloc[k] < st_l.iloc[k]:
                    st_d.iloc[k] = -1
                else:
                    st_d.iloc[k] = st_d.iloc[k-1]
                st.iloc[k] = st_l.iloc[k] if st_d.iloc[k] == 1 else st_u.iloc[k]
            ind['supertrend'] = st
            ind['supertrend_dir'] = st_d
            
            af = 0.02
            max_af = 0.2
            psar = df['close'].copy()
            psar_bull = pd.Series(True, index=df.index)
            ep = df['low'].iloc[0]
            af_cur = af
            for k in range(2, len(df)):
                prev = psar.iloc[k-1]
                bull = psar_bull.iloc[k-1]
                if bull:
                    psar.iloc[k] = prev + af_cur * (ep - prev)
                    psar.iloc[k] = min(psar.iloc[k], df['low'].iloc[k-1], df['low'].iloc[k-2])
                    if df['low'].iloc[k] < psar.iloc[k]:
                        psar_bull.iloc[k] = False
                        psar.iloc[k] = ep
                        ep = df['high'].iloc[k]
                        af_cur = af
                    else:
                        psar_bull.iloc[k] = True
                        if df['high'].iloc[k] > ep:
                            ep = df['high'].iloc[k]
                            af_cur = min(af_cur + af, max_af)
                else:
                    psar.iloc[k] = prev + af_cur * (ep - prev)
                    psar.iloc[k] = max(psar.iloc[k], df['high'].iloc[k-1], df['high'].iloc[k-2])
                    if df['high'].iloc[k] > psar.iloc[k]:
                        psar_bull.iloc[k] = True
                        psar.iloc[k] = ep
                        ep = df['low'].iloc[k]
                        af_cur = af
                    else:
                        psar_bull.iloc[k] = False
                        if df['low'].iloc[k] < ep:
                            ep = df['low'].iloc[k]
                            af_cur = min(af_cur + af, max_af)
            ind['psar'] = psar
            ind['psar_bull'] = psar_bull.astype(float)
            
            ind['dc_upper'] = df['high'].rolling(20).max()
            ind['dc_lower'] = df['low'].rolling(20).min()
            ind['dc_mid'] = (ind['dc_upper'] + ind['dc_lower']) / 2
            
            ind['roc10'] = df['close'].pct_change(10) * 100
            ind['roc5'] = df['close'].pct_change(5) * 100
            ind['mom10'] = df['close'] - df['close'].shift(10)
            ind['mom5'] = df['close'] - df['close'].shift(5)
            
            ind['ema9_slope'] = ind['ema_9'].diff(2) / ind['ema_9'].shift(2).clip(lower=1e-10) * 100
            ind['ema21_slope'] = ind['ema_21'].diff(3) / ind['ema_21'].shift(3).clip(lower=1e-10) * 100
            ind['ema200_slope'] = ind['ema_200'].diff(5) / ind['ema_200'].shift(5).clip(lower=1e-10) * 100
            ind['rsi_slope'] = ind['rsi'].diff(3)
            ind['rsi_slope2'] = ind['rsi'].diff(5)
            
            ind['vol_ma20'] = df['volume'].rolling(20).mean()
            ind['vol_ma50'] = df['volume'].rolling(50).mean()
            ind['vol_ratio_20'] = df['volume'] / ind['vol_ma20'].clip(lower=1e-10)
            ind['vol_ratio_50'] = df['volume'] / ind['vol_ma50'].clip(lower=1e-10)
            ind['vol_zscore'] = (df['volume'] - ind['vol_ma20']) / (df['volume'].rolling(20).std() + 1e-10)
            
            for n in [5, 10, 20]:
                cv = (df['close'] * df['volume']).rolling(n).sum()
                vv = df['volume'].rolling(n).sum().replace(0, np.nan)
                ind[f'vwma_{n}'] = cv / vv.clip(lower=1e-10)
            
            c_body = (df['close'] - df['open']).abs()
            c_range = (df['high'] - df['low']).replace(0, np.nan)
            ind['body_ratio'] = c_body / c_range.clip(lower=1e-10)
            ind['upper_wick_ratio'] = (df['high'] - df[['close', 'open']].max(axis=1)) / c_range.clip(lower=1e-10)
            ind['lower_wick_ratio'] = (df[['close', 'open']].min(axis=1) - df['low']) / c_range.clip(lower=1e-10)
            
            try:
                dt_series = pd.to_datetime(df['date'] if 'date' in df.columns else df.index)
                close_dt = pd.Series(df['close'].values, index=dt_series)
                df15 = close_dt.resample('15min').last().dropna()
                ema9_15 = df15.ewm(span=9, adjust=False).mean()
                ema21_15 = df15.ewm(span=21, adjust=False).mean()
                htf9_vals = ema9_15.reindex(dt_series, method='ffill').values
                htf21_vals = ema21_15.reindex(dt_series, method='ffill').values
                ind['htf_ema9'] = pd.Series(htf9_vals, index=df.index)
                ind['htf_ema21'] = pd.Series(htf21_vals, index=df.index)
                mid = (htf9_vals + htf21_vals) / 2.0
                gap = htf9_vals - htf21_vals
                norm = np.where(mid > 0, gap / mid, 0.0)
                htf_bull_vals = 1.0 / (1.0 + np.exp(-norm * 100))
                ind['htf_bull'] = pd.Series(htf_bull_vals, index=df.index)
            except Exception as e:
                logger.warning(f"HTF calculation error: {e}")
                ind['htf_ema9'] = pd.Series(df['close'].values, index=df.index)
                ind['htf_ema21'] = pd.Series(df['close'].values, index=df.index)
                ind['htf_bull'] = pd.Series(0.5, index=df.index)
            
            return pd.DataFrame(ind)
        except Exception as e:
            logger.error(f"Indicator calculation error: {e}")
            return pd.DataFrame()

    @staticmethod
    def latest(indicators):
        try:
            row = indicators.iloc[-1]
            out = {}
            for col in ['rsi', 'macd', 'macd_signal', 'macd_hist', 'adx', 'plus_di', 'minus_di',
                        'stoch_k', 'stoch_d', 'mfi', 'atr', 'atr_percent', 'bb_position', 'bb_width']:
                if col in row:
                    v = row[col]
                    if not (isinstance(v, float) and np.isnan(v)):
                        out[col] = float(v)
            return out
        except Exception as e:
            logger.error(f"Latest indicators error: {e}")
            return {}

# ==================== STRATEGY VOTING ====================
_STRAT_PERF_MIN_TRADES = 10

def _strategy_perf_weight(name, strategy_performance):
    if not strategy_performance:
        return 1.0
    sp = strategy_performance.get(name)
    if not sp:
        return 1.0
    total = sp.get('total_trades', 0)
    if total < _STRAT_PERF_MIN_TRADES:
        return 1.0
    win_rate = sp.get('win_rate', 0.5)
    return max(0.4, min(1.3, 0.4 + win_rate * 1.2))

def _strat_votes(df_slice, ind_slice, strategies_dict, strategy_performance=None):
    b = 0.0
    s = 0.0
    total = 0
    try:
        for name, func in strategies_dict.items():
            try:
                if func(df_slice, ind_slice):
                    total += 1
                    meta = AVAILABLE_STRATEGY_META.get(name, {})
                    direction = meta.get('direction', 'BOTH')
                    pts = 5.0 if meta.get('high_trust') else 3.0
                    pts *= _strategy_perf_weight(name, strategy_performance)
                    if direction == 'SELL':
                        s += pts
                    elif direction == 'BUY':
                        b += pts
                    else:
                        b += 0.5
                        s += 0.5
            except Exception as e:
                logger.debug(f"Strategy vote error {name}: {e}")
    except Exception as e:
        logger.error(f"Strat votes error: {e}")
    return b, s, total

def _get_strategy_min_bars(strategies_dict, fallback=160):
    seen_modules = set()
    best = None
    for fn in strategies_dict.values():
        module_name = getattr(fn, '__module__', '') or ''
        if not module_name or module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        module_obj = sys.modules.get(module_name)
        if module_obj is None:
            continue
        val = getattr(module_obj, 'MIN_BARS_REQUIRED', None)
        if val is not None:
            best = val if best is None else max(best, val)
    return best if best is not None else fallback

def _get_strategy_timeframe(strategies_dict, fallback='3minute'):
    """
    Reads the candle timeframe each strategy module wants directly off
    that module (a `TIMEFRAME = "3minute"` / "5minute" / "15minute" etc.
    constant, same pattern as MIN_BARS_REQUIRED above) instead of the
    engine hardcoding "3minute" for every strategy regardless of what it
    actually needs. This is what lets each strategy file own its own
    data settings — the engine still owns the single Kite Connect session
    (so calls stay rate-limited/cached and every strategy's data comes
    from one consistent fetch, rather than each strategy file making its
    own uncoordinated broker API calls), but the PARAMETERS of that fetch
    (timeframe, lookback via MIN_BARS_REQUIRED) are entirely strategy-
    declared.

    If multiple loaded strategies declare different timeframes, this
    currently returns the first one found and logs a warning — running
    strategies that need genuinely different timeframes in the same
    scan/backtest at once requires per-timeframe data (a larger change);
    for now, keep all concurrently-active strategies on one timeframe, or
    run them in separate scans/backtests.
    """
    seen_modules = set()
    found = {}
    for fn in strategies_dict.values():
        module_name = getattr(fn, '__module__', '') or ''
        if not module_name or module_name in seen_modules:
            continue
        seen_modules.add(module_name)
        module_obj = sys.modules.get(module_name)
        if module_obj is None:
            continue
        val = getattr(module_obj, 'TIMEFRAME', None)
        if val is not None:
            found[module_name] = val
    if not found:
        return fallback
    distinct = set(found.values())
    if len(distinct) > 1:
        logger.warning(
            f"Strategies declare different TIMEFRAMEs {found} — using "
            f"'{next(iter(distinct))}' for this scan. Run strategies that "
            f"need different timeframes separately."
        )
    return next(iter(distinct))

def _lookback_days(timeframe, min_bars_needed, floor_days=6):
    """
    How many calendar days of history to request for a given candle
    timeframe so that at least min_bars_needed completed bars are
    available, with a small buffer for holidays/weekends. Used so that
    switching a strategy's TIMEFRAME (see _get_strategy_timeframe) also
    correctly scales the historical_data() lookback window, instead of
    the engine hardcoding "6 days" for every timeframe (which is right
    for 3minute but far too short for, say, a daily-timeframe strategy).
    """
    minutes_per_bar = {
        'minute': 1, '3minute': 3, '5minute': 5, '10minute': 10,
        '15minute': 15, '30minute': 30, '60minute': 60,
    }.get(timeframe, 3)
    if timeframe == 'day':
        # ~1.6 calendar days per trading day (weekends/holidays buffer)
        return max(floor_days, int(min_bars_needed * 1.6) + 5)
    trading_minutes_per_day = 375  # NSE 9:15-15:30
    bars_per_day = max(1, trading_minutes_per_day // minutes_per_bar)
    import math
    days_needed = math.ceil(min_bars_needed / bars_per_day) + 2  # +2 buffer days
    return max(floor_days, days_needed)

# ==================== CANDLE PATTERN ENGINE ====================
def detect_candle_patterns(df, i):
    patterns = {}
    buy_bonus = 0.0
    sell_bonus = 0.0
    try:
        c = float(df['close'].iloc[i])
        o = float(df['open'].iloc[i])
        h = float(df['high'].iloc[i])
        l = float(df['low'].iloc[i])
        body = abs(c - o)
        rng = (h - l) if (h - l) > 0 else 1e-9
        body_r = body / rng
        uw_r = (h - max(c, o)) / rng
        lw_r = (min(c, o) - l) / rng
        bull = c > o
        bear = c < o
        patterns['DOJI'] = body_r < 0.10
        patterns['SPINNING_TOP'] = 0.10 <= body_r <= 0.30 and uw_r > 0.25 and lw_r > 0.25
        patterns['HAMMER'] = (lw_r > 0.60 and body_r < 0.30 and uw_r < 0.15 and bull)
        patterns['INVERTED_HAMMER'] = (uw_r > 0.60 and body_r < 0.30 and lw_r < 0.15 and bull)
        patterns['SHOOTING_STAR'] = (uw_r > 0.60 and body_r < 0.30 and lw_r < 0.15 and bear)
        patterns['HANGING_MAN'] = (lw_r > 0.60 and body_r < 0.30 and uw_r < 0.15 and bear)
        patterns['BULL_MARUBOZU'] = (body_r > 0.85 and bull and uw_r < 0.08 and lw_r < 0.08)
        patterns['BEAR_MARUBOZU'] = (body_r > 0.85 and bear and uw_r < 0.08 and lw_r < 0.08)
        patterns['STRONG_BULL_BODY'] = (body_r > 0.65 and bull and uw_r < 0.20)
        patterns['STRONG_BEAR_BODY'] = (body_r > 0.65 and bear and lw_r < 0.20)
        if i >= 1:
            pc = float(df['close'].iloc[i-1])
            po = float(df['open'].iloc[i-1])
            ph = float(df['high'].iloc[i-1])
            pl = float(df['low'].iloc[i-1])
            pbull = pc > po
            pbear = pc < po
            prev_body = abs(pc - po)
            patterns['BULL_ENGULFING'] = (pbear and bull and o < pc and c > po and body > prev_body * 1.0)
            patterns['BEAR_ENGULFING'] = (pbull and bear and o > pc and c < po and body > prev_body * 1.0)
            prev_mid = (po + pc) / 2
            patterns['PIERCING_LINE'] = (pbear and bull and o < pl and c > prev_mid and c < po)
            patterns['DARK_CLOUD_COVER'] = (pbull and bear and o > ph and c < prev_mid and c > pc)
            patterns['TWEEZER_TOP'] = (pbull and bear and abs(h - ph) / rng < 0.05)
            patterns['TWEEZER_BOTTOM'] = (pbear and bull and abs(l - pl) / rng < 0.05)
        if i >= 2:
            c2 = float(df['close'].iloc[i-2])
            o2 = float(df['open'].iloc[i-2])
            c1 = float(df['close'].iloc[i-1])
            o1 = float(df['open'].iloc[i-1])
            body1 = abs(c1 - o1)
            rng1 = (float(df['high'].iloc[i-1]) - float(df['low'].iloc[i-1])) if (float(df['high'].iloc[i-1]) - float(df['low'].iloc[i-1])) > 0 else 1e-9
            mid1_body = body1 / rng1
            bar2_mid = (o2 + c2) / 2
            patterns['MORNING_STAR'] = (c2 < o2 and mid1_body < 0.30 and bull and c > bar2_mid)
            patterns['EVENING_STAR'] = (c2 > o2 and mid1_body < 0.30 and bear and c < bar2_mid)
            patterns['THREE_WHITE_SOLDIERS'] = (c2 > o2 and c1 > o1 and bull and c1 > c2 and c > c1 and body_r > 0.50 and (abs(c2 - o2) / ((float(df['high'].iloc[i-2]) - float(df['low'].iloc[i-2])) or 1)) > 0.50)
            patterns['THREE_BLACK_CROWS'] = (c2 < o2 and c1 < o1 and bear and c1 < c2 and c < c1 and body_r > 0.50 and (abs(c2 - o2) / ((float(df['high'].iloc[i-2]) - float(df['low'].iloc[i-2])) or 1)) > 0.50)
        for p in ['HAMMER','INVERTED_HAMMER','BULL_ENGULFING','PIERCING_LINE','MORNING_STAR','THREE_WHITE_SOLDIERS','TWEEZER_BOTTOM','BULL_MARUBOZU']:
            if patterns.get(p):
                pts = {'BULL_ENGULFING':6,'MORNING_STAR':7,'THREE_WHITE_SOLDIERS':6,'BULL_MARUBOZU':5,'HAMMER':5,'PIERCING_LINE':4,'TWEEZER_BOTTOM':4,'INVERTED_HAMMER':3}
                buy_bonus += pts.get(p, 3)
        for p in ['SHOOTING_STAR','HANGING_MAN','BEAR_ENGULFING','DARK_CLOUD_COVER','EVENING_STAR','THREE_BLACK_CROWS','TWEEZER_TOP','BEAR_MARUBOZU']:
            if patterns.get(p):
                pts = {'BEAR_ENGULFING':6,'EVENING_STAR':7,'THREE_BLACK_CROWS':6,'BEAR_MARUBOZU':5,'SHOOTING_STAR':5,'DARK_CLOUD_COVER':4,'TWEEZER_TOP':4,'HANGING_MAN':3}
                sell_bonus += pts.get(p, 3)
        if patterns.get('DOJI') or patterns.get('SPINNING_TOP'):
            buy_bonus -= 8.0
            sell_bonus -= 8.0
    except Exception as e:
        logger.debug(f"Candle pattern error: {e}")
    return patterns, buy_bonus, sell_bonus

# ==================== PAPER TRADING ENGINE ====================
class PaperTradingEngine:
    # These four are only the *fallback defaults* shown in Settings the first
    # time a user opens it — actual live values come from the per-user,
    # per-mode risk config (see UserManager.get_user_risk_config /
    # /api/user/update-risk) and are stored on the instance as
    # self.target_pct / self.stoploss_pct / self.max_target_pct / self.max_sl_pct.
    TARGET_PCT = 0.010
    STOPLOSS_PCT = 0.005
    TARGET_PCT_INTRADAY_DEFAULT_UI = 1.0     # percent, i.e. 1.0 = 1%
    STOPLOSS_PCT_INTRADAY_DEFAULT_UI = 0.5
    TARGET_PCT_DELIVERY_DEFAULT_UI = 3.0     # CNC/swing trades want more room
    STOPLOSS_PCT_DELIVERY_DEFAULT_UI = 1.5
    INTRADAY_MARGIN_PCT = 0.20
    MAX_OPEN_POS = 1
    WALLET_USAGE_PCT = 0.80
    MIN_PRICE = 100.0
    SLIPPAGE_PCT = 0.001
    MIN_ABSOLUTE_MOVE = 0.50
    STRATEGY_MIN_TRADES = 10
    STRATEGY_MIN_WIN_RATE = 0.40
    MAX_DAILY_LOSS_PCT = 0.05
    MAX_DAILY_PROFIT_PCT = 0.02
    MAX_CONSECUTIVE_LOSSES = 3
    COOLDOWN_MINUTES = 5
    CIRCUIT_BREAKER_THRESHOLD = 0.10
    MARKET_OPEN = 555
    MARKET_CLOSE = 930
    NO_NEW_TRADES_AFTER = 870
    SQUARE_OFF_TIME = 915
    # Minutes after MARKET_OPEN before new (non-open-position) entries are
    # first evaluated — was 15 (9:15-9:30 blacked out), now 3 so live/backtest
    # entries can start from 9:18 instead of 9:30.
    NEW_ENTRY_WARMUP_MINUTES = 3
    MIN_SIGNAL_SCORE = 35.0
    MIN_VOTE_PCT = 50.0
    MIN_VOL_SURGE = 1.3
    COUNTER_TREND_SCORE_BOOST = 15.0
    MIN_HTF_ALIGN_SCORE = 42.0
    RISK_PER_TRADE_PCT = 0.01
    ATR_SL_MULTIPLIER = 1.5
    SECTOR_BIAS_SCORE = 6.0
    SECTOR_BIAS_TTL = 300
    DIAG_LOG_COOLDOWN_SECONDS = 300
    # _save() runs on almost every signal-log line (multiple times a second
    # while monitoring is active), but a backup COPY of the whole paper file
    # is much heavier and only useful as periodic point-in-time snapshots —
    # not something that needs to happen on every single save. Throttling
    # the backup itself (independent of the live file write, which still
    # happens every time) means the 100-file retention cap in _backup_file
    # actually spans hours of history instead of a couple of minutes.
    BACKUP_INTERVAL_SECONDS = 300
    
    def __init__(self, config, strategies_dict, trading_mode='INTRADAY', strategy_name=None, risk_config=None):
        self.config = config
        self.strategies_dict = strategies_dict
        self.trading_mode = trading_mode if trading_mode in ('INTRADAY', 'DELIVERY') else 'INTRADAY'
        self.strategy_name = strategy_name
        # Target/SL are user-configurable per trading mode (see Settings ->
        # Target & Stop Loss), not fixed constants. risk_config holds UI-scale
        # percents (1.0 == 1%); convert to fractions and pick the pair that
        # matches this engine's trading mode.
        rc = risk_config or {}
        if self.trading_mode == 'DELIVERY':
            tgt_ui = rc.get('target_pct_delivery', self.TARGET_PCT_DELIVERY_DEFAULT_UI)
            sl_ui = rc.get('stoploss_pct_delivery', self.STOPLOSS_PCT_DELIVERY_DEFAULT_UI)
        else:
            tgt_ui = rc.get('target_pct_intraday', self.TARGET_PCT_INTRADAY_DEFAULT_UI)
            sl_ui = rc.get('stoploss_pct_intraday', self.STOPLOSS_PCT_INTRADAY_DEFAULT_UI)
        try:
            tgt_ui = float(tgt_ui)
        except (TypeError, ValueError):
            tgt_ui = self.TARGET_PCT_DELIVERY_DEFAULT_UI if self.trading_mode == 'DELIVERY' else self.TARGET_PCT_INTRADAY_DEFAULT_UI
        try:
            sl_ui = float(sl_ui)
        except (TypeError, ValueError):
            sl_ui = self.STOPLOSS_PCT_DELIVERY_DEFAULT_UI if self.trading_mode == 'DELIVERY' else self.STOPLOSS_PCT_INTRADAY_DEFAULT_UI
        self.target_pct = max(0.0005, min(0.20, tgt_ui / 100.0))
        self.stoploss_pct = max(0.0005, min(0.20, sl_ui / 100.0))
        # Hard ceiling above the configured target/SL — keeps the ATR-scaled
        # branch of _calculate_atr_targets from running away on volatile
        # bars, sized proportionally to whatever the user configured instead
        # of a fixed absolute number.
        self.max_target_pct = round(self.target_pct * 1.5, 4)
        self.max_sl_pct = round(self.stoploss_pct * 1.6, 4)
        # CNC/Delivery: minimum calendar days a position must be held before
        # the algo is allowed to auto-exit it on TARGET/STOP_LOSS — mirrors
        # real swing/investment behavior instead of same-session flips.
        # User-controlled via Settings -> Target & Stop Loss. Manual exits
        # (the Exit button) are NOT blocked by this — it only gates the
        # automated monitor loop.
        if self.trading_mode == 'DELIVERY':
            try:
                mhd = int(rc.get('min_hold_days_delivery', 1))
            except (TypeError, ValueError):
                mhd = 1
            self.min_hold_days = max(0, min(30, mhd))
        else:
            self.min_hold_days = 0
        self.risk_config = rc
        self.PAPER_FILE = config.PAPER_FILE
        self.PAPER_BACKUP_DIR = config.PAPER_BACKUP_DIR
        os.makedirs(self.PAPER_BACKUP_DIR, exist_ok=True)
        self._lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._last_backup_time = 0.0
        self.data = self._load()
        self._monitor_thread = None
        self._running = False
        self._last_heartbeat = datetime.now()
        self.active_stock_orders = {}
        self.last_exit_time = {}
        self.consecutive_losses = 0
        self.last_trade_pnl = None
        self.strategy_performance = self.data.get('strategy_performance', {})
        self._signal_logs = deque(maxlen=5000)
        self._margin_cache = {}
        self._margin_cache_time = {}
        self._margin_cache_ttl = 300
        self._fast_exit_thread = None
        self._sector_bias_cache = {}
        self._diag_log_cooldown = {}
        logger.info(f"PaperTradingEngine initialized [mode={self.trading_mode}] [strategy={self.strategy_name}]")

    def _log_diag(self, key, entry):
        """Log a low-value/repetitive diagnostic rejection (e.g. price too low,
        insufficient data) at most once per cooldown window per key, so a
        permanently-unqualified monitored stock can't flood the signal log every
        cycle and evict real signals for other symbols."""
        now_ts = time.time()
        with self._lock:
            last = self._diag_log_cooldown.get(key, 0)
            if now_ts - last < self.DIAG_LOG_COOLDOWN_SECONDS:
                return
            self._diag_log_cooldown[key] = now_ts
        self._add_signal_log(entry)

    def _fast_exit_loop(self):
        logger.info("Fast exit loop started")
        while self._running:
            try:
                now = datetime.now()
                mins = now.hour * 60 + now.minute
                if not (self.MARKET_OPEN <= mins <= self.MARKET_CLOSE):
                    time.sleep(1)
                    continue

                with self._lock:
                    positions = dict(self.data.get("positions", {}))
                if not positions:
                    time.sleep(1)
                    continue

                syms = list(positions.keys())
                batch_ltp = self._get_batch_ltp(syms)
                if not batch_ltp:
                    time.sleep(1)
                    continue

                with self._lock:
                    for sym, pos in list(self.data.get("positions", {}).items()):
                        ltp = batch_ltp.get(sym)
                        if ltp is None:
                            continue

                        side = pos["side"]
                        target = pos.get("target")
                        stoploss = pos.get("stoploss")
                        if target is None or stoploss is None:
                            continue

                        if self.trading_mode == 'DELIVERY' and self.min_hold_days > 0:
                            try:
                                held_days = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 86400
                            except Exception:
                                held_days = self.min_hold_days
                            if held_days < self.min_hold_days:
                                continue

                        exit_price = None
                        reason = None
                        if side == "BUY":
                            if ltp >= target:
                                exit_price = ltp
                                reason = "FAST_TARGET"
                            elif ltp <= stoploss:
                                exit_price = ltp
                                reason = "FAST_STOP_LOSS"
                        else:
                            if ltp <= target:
                                exit_price = ltp
                                reason = "FAST_TARGET"
                            elif ltp >= stoploss:
                                exit_price = ltp
                                reason = "FAST_STOP_LOSS"

                        if exit_price:
                            logger.info(f"FAST EXIT {sym} {side} {reason} @ {exit_price:.2f}")
                            self._close_position_nolock(sym, exit_price=exit_price, reason=reason)

                time.sleep(1)
            except Exception as e:
                logger.error(f"Fast exit loop error: {e}")
                time.sleep(5)
    
    def _backup_file(self, filepath, force=False):
        try:
            now_ts = time.time()
            if not force and (now_ts - self._last_backup_time) < self.BACKUP_INTERVAL_SECONDS:
                return
            if os.path.exists(filepath):
                self._last_backup_time = now_ts
                # os.path.join here (not an f-string '/') keeps the whole path
                # using one consistent separator convention on every OS —
                # mixing a literal '/' with os.path.join's native '\' on
                # Windows produced malformed paths like
                # "data\\rahul\\backups/paper_...json" that could fail with
                # WinError 2 depending on how the OS/AV layer parses them.
                backup_name = os.path.join(
                    self.PAPER_BACKUP_DIR,
                    f"paper_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json",
                )
                os.makedirs(self.PAPER_BACKUP_DIR, exist_ok=True)
                shutil.copy(filepath, backup_name)
                backups = sorted(glob.glob(os.path.join(self.PAPER_BACKUP_DIR, "paper_*.json")))
                for old in backups[:-100]:
                    try:
                        os.remove(old)
                    except FileNotFoundError:
                        pass  # already removed by a concurrent cleanup pass
        except Exception as e:
            logger.error(f"Backup error: {e}")
    
    def _load(self):
        try:
            if os.path.exists(self.PAPER_FILE):
                with open(self.PAPER_FILE, 'r') as f:
                    data = json.load(f)
                # NOTE: 'pinned'/'pinned_meta' are vestigial — the Monitored/
                # pin feature was removed (the engine now always scans the
                # full NIFTY 200 universe, see NIFTY200_SYMBOLS). These keys
                # are kept only so old paper_*.json files from before this
                # change still load without a KeyError; nothing reads or
                # writes them anymore.
                defaults = {
                    'wallet': 10000.0,
                    'pinned': [],
                    'pinned_meta': {},
                    'positions': {},
                    'orders': [],
                    'trades': [],
                    'daily_pnl': {},
                    'signal_logs': [],
                    'strategy_performance': {},
                    'circuit_breaker': {'triggered': False, 'time': None, 'reason': None},
                    'daily_stats': {
                        'date': datetime.now().strftime('%Y-%m-%d'),
                        'pnl': 0.0,
                        'trades': 0,
                        'wins': 0,
                        'losses': 0
                    }
                }
                for k, v in defaults.items():
                    if k not in data:
                        data[k] = v
                return data
        except Exception as e:
            logger.error(f"Load error: {e}")
        return {
            'wallet': 10000.0,
            'pinned': [],
            'pinned_meta': {},
            'positions': {},
            'orders': [],
            'trades': [],
            'daily_pnl': {},
            'signal_logs': [],
            'strategy_performance': {},
            'circuit_breaker': {'triggered': False, 'time': None, 'reason': None},
            'daily_stats': {'date': datetime.now().strftime('%Y-%m-%d'), 'pnl': 0.0, 'trades': 0, 'wins': 0, 'losses': 0}
        }
    
    def _save(self, force_backup=False):
        with self._lock:
            try:
                # The live file is always written every time _save() runs.
                # The backup COPY is throttled (see BACKUP_INTERVAL_SECONDS) —
                # force_backup=True skips the throttle for events worth an
                # immediate point-in-time snapshot (position opened/closed).
                self._backup_file(self.PAPER_FILE, force=force_backup)
                with open(self.PAPER_FILE, 'w') as f:
                    json.dump(self.data, f, indent=2, cls=DateTimeEncoder)
            except Exception as e:
                logger.error(f"Save error: {e}")
    
    def set_wallet(self, amount):
        with self._lock:
            self.data['wallet'] = round(float(amount), 2)
            self._save()
    
    def _check_circuit_breakers(self):
        with self._lock:
            now = datetime.now()
            cb = self.data.get('circuit_breaker', {})
            if cb.get('triggered'):
                t = cb.get('time')
                if t:
                    t = datetime.fromisoformat(t) if isinstance(t, str) else t
                    if (now - t).total_seconds() > 1800:
                        self.data['circuit_breaker']['triggered'] = False
                        self.data['circuit_breaker']['reason'] = None
                    else:
                        return False, "Circuit breaker active"
            today = now.strftime('%Y-%m-%d')
            daily_stats = self.data['daily_pnl'].get(today, {})
            daily_pnl = daily_stats.get('realized', 0)
            initial_capital = daily_stats.get('peak_wallet', self.data['wallet'])
            if initial_capital <= 0:
                initial_capital = self.data['wallet']
            if daily_pnl < 0 and abs(daily_pnl) > initial_capital * self.MAX_DAILY_LOSS_PCT:
                self._trigger_circuit_breaker(f"Daily loss limit: {daily_pnl:.2f}")
                return False, "Daily loss limit exceeded"
            if daily_pnl > 0 and daily_pnl > initial_capital * self.MAX_DAILY_PROFIT_PCT:
                self._trigger_circuit_breaker(f"Daily profit target reached: {daily_pnl:.2f}")
                return False, "Daily profit target reached"
            if self.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
                self._trigger_circuit_breaker(f"{self.consecutive_losses} consecutive losses")
                return False, "Max consecutive losses"
            trade_peaks = [t.get('peak_wallet', 0) for t in self.data['trades'][-50:]]
            peak = max([self.data['wallet']] + trade_peaks)
            if peak > 0 and (peak - self.data['wallet']) / peak > self.CIRCUIT_BREAKER_THRESHOLD:
                self._trigger_circuit_breaker("Max drawdown exceeded")
                return False, "Max drawdown exceeded"
            return True, "OK"
    
    def _trigger_circuit_breaker(self, reason):
        with self._lock:
            self.data['circuit_breaker'] = {
                'triggered': True,
                'time': datetime.now().isoformat(),
                'reason': reason
            }
            logger.warning(f"CIRCUIT BREAKER: {reason}")
            if self.data['positions']:
                self._squareoff_all()
            self._save()
    
    def _get_live_ltp(self, symbol, retries=3):
        for attempt in range(retries):
            try:
                _quote_limiter.wait(f"ltp {symbol}")
                q = self._kite.ltp([f"NSE:{symbol}"])
                if q and f"NSE:{symbol}" in q:
                    return float(q[f"NSE:{symbol}"]["last_price"])
            except Exception as e:
                if attempt < retries - 1:
                    time.sleep(1.1)
                else:
                    logger.error(f"LTP failed {symbol}: {e}")
        return None

    def _get_batch_ltp(self, symbols, retries=3):
        if not symbols:
            return {}
        BATCH_SIZE = 500
        result = {}
        for i in range(0, len(symbols), BATCH_SIZE):
            batch = symbols[i : i + BATCH_SIZE]
            keys = [f"NSE:{s}" for s in batch]
            for attempt in range(retries):
                try:
                    _quote_limiter.wait(f"batch_ltp chunk {i//BATCH_SIZE}")
                    q = self._kite.ltp(keys)
                    for s in batch:
                        k = f"NSE:{s}"
                        result[s] = float(q[k]["last_price"]) if q and k in q else None
                    break
                except Exception as e:
                    if attempt < retries - 1:
                        time.sleep(1.1)
                    else:
                        logger.error(f"Batch LTP failed for chunk starting {batch[0]}: {e}")
                        for s in batch:
                            result[s] = None
        return result
    
    @property
    def _kite(self):
        return self._kite_instance

    @_kite.setter
    def _kite(self, kite):
        self._kite_instance = kite

    def _get_execution_price(self, symbol, side, trigger_price, is_stop_loss=False):
        ltp = self._get_live_ltp(symbol)
        if ltp is None:
            return trigger_price
        if is_stop_loss:
            if side == 'SELL':
                execution_price = min(ltp, trigger_price)
            else:
                execution_price = max(ltp, trigger_price)
        else:
            execution_price = ltp
        if side == 'BUY':
            execution_price *= (1.0 + self.SLIPPAGE_PCT)
        else:
            execution_price *= (1.0 - self.SLIPPAGE_PCT)
        return round(execution_price, 2)
    
    def _check_reversal_exit(self, symbol, pos, df, ind):
        """
        Give the strategy that opened this position a chance to say "my
        setup is invalidated, get out now" — ahead of target/SL. Fully
        delegated to the strategy module via strategy_exits: the scanner
        doesn't know or care whether that means an EMA crossing back,
        an RSI exhaustion signal, or anything else — it just calls
        whatever function the strategy registered and acts on True/False.
        Strategies that don't register anything here are skipped entirely.
        """
        exit_fn = AVAILABLE_STRATEGY_EXITS.get(pos.get('strategy'))
        if not exit_fn:
            return None, None
        try:
            should_exit = exit_fn(df, ind, pos)
        except Exception as e:
            logger.error(f"Strategy exit check error [{pos.get('strategy')}] {symbol}: {e}")
            return None, None
        if not should_exit:
            return None, None

        side = pos['side']
        exit_side = 'SELL' if side == 'BUY' else 'BUY'
        ep = self._get_execution_price(symbol, exit_side, 0)
        logger.info(
            f"STRATEGY_EXIT {symbol}: strategy={pos.get('strategy')} side={side} -> {ep:.2f}"
        )
        return ep, 'STRATEGY_EXIT'

    def _check_stop_loss_target(self, symbol, pos, df, ltp):
        entry = pos['entry_price']
        side = pos['side']
        target = pos.get('target')
        stoploss = pos.get('stoploss')
        if target is None or stoploss is None:
            return None, None
        if ltp is not None:
            if side == 'BUY':
                if ltp >= target:
                    ep = self._get_execution_price(symbol, 'SELL', target)
                    logger.info(f"TARGET HIT {symbol} via LTP: {ltp:.2f}>=T={target:.2f} -> {ep:.2f}")
                    return ep, 'TARGET'
                if ltp <= stoploss:
                    ep = self._get_execution_price(symbol, 'SELL', stoploss, is_stop_loss=True)
                    logger.info(f"SL HIT {symbol} via LTP: {ltp:.2f}<=SL={stoploss:.2f} -> {ep:.2f}")
                    return ep, 'STOP_LOSS'
            else:
                if ltp <= target:
                    ep = self._get_execution_price(symbol, 'BUY', target)
                    logger.info(f"TARGET HIT {symbol} via LTP: {ltp:.2f}<=T={target:.2f} -> {ep:.2f}")
                    return ep, 'TARGET'
                if ltp >= stoploss:
                    ep = self._get_execution_price(symbol, 'BUY', stoploss, is_stop_loss=True)
                    logger.info(f"SL HIT {symbol} via LTP: {ltp:.2f}>=SL={stoploss:.2f} -> {ep:.2f}")
                    return ep, 'STOP_LOSS'
            return None, None
        try:
            if 'date' in df.columns and not pd.api.types.is_datetime64_any_dtype(df['date']):
                df = df.copy()
                df['date'] = pd.to_datetime(df['date'])
            entry_time = None
            try:
                entry_time = datetime.fromisoformat(pos['entry_time'])
            except Exception:
                pass
            if 'date' in df.columns and entry_time is not None:
                mask = df['date'] > entry_time
                if mask.sum() < 1:
                    return None, None
                ped = df[mask]
            else:
                ped = df.iloc[-1:]
            df_low = float(ped['low'].min())
            df_high = float(ped['high'].max())
            if side == 'BUY':
                if df_high >= target:
                    ep = self._get_execution_price(symbol, 'SELL', target)
                    logger.info(f"TARGET HIT {symbol} via candle High={df_high:.2f}>=T={target:.2f}")
                    return ep, 'TARGET'
                if df_low <= stoploss:
                    ep = self._get_execution_price(symbol, 'SELL', stoploss, is_stop_loss=True)
                    logger.info(f"SL HIT {symbol} via candle Low={df_low:.2f}<=SL={stoploss:.2f}")
                    return ep, 'STOP_LOSS'
            else:
                if df_low <= target:
                    ep = self._get_execution_price(symbol, 'BUY', target)
                    logger.info(f"TARGET HIT {symbol} via candle Low={df_low:.2f}<=T={target:.2f}")
                    return ep, 'TARGET'
                if df_high >= stoploss:
                    ep = self._get_execution_price(symbol, 'BUY', stoploss, is_stop_loss=True)
                    logger.info(f"SL HIT {symbol} via candle High={df_high:.2f}>=SL={stoploss:.2f}")
                    return ep, 'STOP_LOSS'
        except Exception as e:
            logger.error(f"Stop loss check error {symbol}: {e}")
        return None, None
    
    def _update_trailing_stop(self, pos, ltp, current_move_pct, mins):
        side = pos['side']
        entry = pos['entry_price']
        updated = False
        atr = pos.get('entry_atr')
        if atr and atr > 0:
            peak_price = pos.get('peak_price', entry)
            if side == 'BUY':
                new_sl = round(peak_price - 1.5 * atr, 2)
                if new_sl > pos.get('stoploss', 0):
                    pos['stoploss'] = new_sl
                    updated = True
                    logger.info(f"ATR trail LONG {entry:.2f} -> SL {new_sl:.2f} (peak {peak_price:.2f})")
            else:
                new_sl = round(peak_price + 1.5 * atr, 2)
                if new_sl < pos.get('stoploss', float('inf')):
                    pos['stoploss'] = new_sl
                    updated = True
                    logger.info(f"ATR trail SHORT {entry:.2f} -> SL {new_sl:.2f} (peak {peak_price:.2f})")
        if current_move_pct >= 0.3:
            if not pos.get('trail_activated', False):
                pos['trail_activated'] = True
                if side == 'BUY':
                    new_sl = round(entry * 1.001, 2)
                    if new_sl > pos.get('stoploss', 0):
                        pos['stoploss'] = new_sl
                        updated = True
                        logger.info(f"Trail activated LONG {entry:.2f} -> SL {new_sl:.2f}")
                else:
                    new_sl = round(entry * 0.999, 2)
                    if new_sl < pos.get('stoploss', float('inf')):
                        pos['stoploss'] = new_sl
                        updated = True
                        logger.info(f"Trail activated SHORT {entry:.2f} -> SL {new_sl:.2f}")
            if current_move_pct >= 1.0:
                if side == 'BUY':
                    candidate = max(round(entry * 1.003, 2), round(ltp * 0.995, 2))
                    if candidate > pos.get('stoploss', 0):
                        pos['stoploss'] = candidate
                        updated = True
                        logger.info(f"Trail tightened LONG -> SL {candidate:.2f} (ltp={ltp:.2f})")
                else:
                    candidate = min(round(entry * 0.997, 2), round(ltp * 1.005, 2))
                    if candidate < pos.get('stoploss', float('inf')):
                        pos['stoploss'] = candidate
                        updated = True
                        logger.info(f"Trail tightened SHORT -> SL {candidate:.2f} (ltp={ltp:.2f})")
        # EOD stop-loss tightening only makes sense for INTRADAY (MIS)
        # positions that must be squared off by 15:15 — squeezing the SL
        # toward LTP at 14:30 on a DELIVERY/CNC position would force it
        # closed the same day it was opened, which is exactly the
        # intraday-style forced-exit behavior CNC must not have.
        if self.trading_mode == 'INTRADAY' and mins >= 870:
            if side == 'BUY':
                eod_sl = round(ltp * 0.998, 2)
                if eod_sl > pos.get('stoploss', 0):
                    pos['stoploss'] = eod_sl
                    updated = True
            else:
                eod_sl = round(ltp * 1.002, 2)
                if eod_sl < pos.get('stoploss', float('inf')):
                    pos['stoploss'] = eod_sl
                    updated = True
        if side == 'BUY':
            if ltp > pos.get('peak_price', entry):
                pos['peak_price'] = ltp
        else:
            if ltp < pos.get('peak_price', entry):
                pos['peak_price'] = ltp
        return updated
    
    def _get_actual_margin(self, symbol, price, side="BUY"):
        now = time.time()
        key = f"{symbol}_{side}_{self.trading_mode}"
        if (
            key in self._margin_cache
            and (now - self._margin_cache_time.get(key, 0)) < self._margin_cache_ttl
        ):
            cached = self._margin_cache[key]
            logger.debug(
                f"Margin cache hit {symbol} {side} [{self.trading_mode}]: "
                f"{cached[0]:.2f}/hare ({cached[1]*100:.1f}%)"
            )
            return cached

        if self.trading_mode == 'DELIVERY':
            # Delivery (CNC) uses full cash per share — no leverage.
            margin_per_share = round(price, 4)
            margin_pct = 1.0
            self._margin_cache[key] = (margin_per_share, margin_pct, "delivery_cash")
            self._margin_cache_time[key] = now
            logger.info(f"Margin {symbol} {side} [DELIVERY]: full cash {margin_per_share:.2f}/share (1x)")
            return margin_per_share, margin_pct, "delivery_cash"

        try:
            _other_limiter.wait(f"margin {symbol}")
            result = self._kite.order_margins(
                [
                    {
                        "exchange": "NSE",
                        "tradingsymbol": symbol,
                        "transaction_type": side,
                        "variety": "regular",
                        "product": "MIS",
                        "order_type": "MARKET",
                        "quantity": 1,
                    }
                ]
            )
            if result and len(result) > 0:
                margin_per_share = float(result[0].get("total", 0))
                if margin_per_share > 0 and price > 0:
                    margin_pct = round(margin_per_share / price, 4)
                    margin_pct = max(0.10, min(1.0, margin_pct))
                    margin_per_share = round(price * margin_pct, 4)
                    leverage = round(result[0].get("leverage", 1 / margin_pct), 2)
                    logger.info(
                        f"Margin {symbol} {side} [INTRADAY]: {margin_per_share:.2f}/share "
                        f"({margin_pct*100:.1f}% = {leverage}x) — Kite API"
                    )
                    self._margin_cache[key] = (margin_per_share, margin_pct, "kite_api")
                    self._margin_cache_time[key] = now
                    return margin_per_share, margin_pct, "kite_api"
        except Exception as e:
            logger.error(f"Margin API error {symbol}: {e}")

        margin_pct = self.INTRADAY_MARGIN_PCT
        margin_per_share = round(price * margin_pct, 4)
        self._margin_cache[key] = (margin_per_share, margin_pct, "fallback")
        self._margin_cache_time[key] = now
        logger.info(
            f"Margin {symbol} {side} [INTRADAY]: fallback "
            f"{margin_pct*100:.0f}% = {margin_per_share:.2f}/hare"
        )
        return margin_per_share, margin_pct, "fallback"
    
    def _smart_allocation_time_based(self, price, signal_time, symbol=None, side='BUY', atr=None):
        wallet = self.data['wallet']
        usable = wallet * self.WALLET_USAGE_PCT
        if symbol:
            margin_per_share, margin_pct, source = self._get_actual_margin(symbol, price, side)
        else:
            margin_per_share = round(price * self.INTRADAY_MARGIN_PCT, 4)
            margin_pct = self.INTRADAY_MARGIN_PCT
            source = 'fallback'
        if margin_per_share <= 0:
            return 0, 0.0, margin_pct, source

        margin_qty = int(usable // margin_per_share)

        # NOTE: ATR-based risk-per-trade sizing used to be able to cap qty
        # well below the 80%-wallet margin cap (e.g. on low-ATR stocks,
        # 1% of wallet ÷ small SL distance could be far fewer shares than
        # the wallet could otherwise afford). Per product decision, sizing
        # now always uses the full 80%-of-wallet margin cap; ATR still
        # drives the target/stoploss *price levels* elsewhere, just not qty.
        qty = margin_qty
        if atr and atr > 0:
            risk_amount = wallet * self.RISK_PER_TRADE_PCT
            sl_distance = atr * self.ATR_SL_MULTIPLIER
            if sl_distance > 0:
                risk_qty = int(risk_amount / sl_distance)
                logger.info(
                    f"Sizing [{symbol}]: margin_cap={margin_qty} (80% wallet) "
                    f"| risk_qty would have been {risk_qty} (ATR-based, no longer applied) "
                    f"| final={qty}"
                )

        if qty < 1:
            return 0, 0.0, margin_pct, source
        margin_used = round(qty * margin_per_share, 2)
        return qty, margin_used, margin_pct, source
    
    def _get_l2_execution_price(self, symbol, side):
        try:
            _quote_limiter.wait(f"l2_quote {symbol}")
            q = self._kite.quote([f"NSE:{symbol}"])
            if not q or f"NSE:{symbol}" not in q:
                raise ValueError("No quote returned")
            d = q[f"NSE:{symbol}"]
            ltp = float(d["last_price"])
            depth = d.get("depth", {})
            if side == "BUY":
                sells = depth.get("sell", [])
                ask = float(sells[0]["price"]) if sells and sells[0].get("price") else 0.0
                if ask > 0 and ask >= ltp:
                    spread = (ask - ltp) / ltp
                    slip = max(spread, self.SLIPPAGE_PCT)
                    fp = round(ask, 2)
                else:
                    slip = self.SLIPPAGE_PCT
                    fp = round(ltp * (1 + slip), 2)
            else:
                buys = depth.get("buy", [])
                bid = float(buys[0]["price"]) if buys and buys[0].get("price") else 0.0
                if bid > 0 and bid <= ltp:
                    spread = (ltp - bid) / ltp
                    slip = max(spread, self.SLIPPAGE_PCT)
                    fp = round(bid, 2)
                else:
                    slip = self.SLIPPAGE_PCT
                    fp = round(ltp * (1 - slip), 2)
            logger.info(
                f"L2 fill {symbol} {side}: LTP={ltp:.2f} → fill={fp:.2f}"
                f" slip={slip*100:.3f}%"
            )
            return fp, round(slip * 100, 4)
        except Exception as e:
            logger.debug(f"L2 quote fallback {symbol}: {e}")
            ltp = self._get_live_ltp(symbol) or 0.0
            if ltp <= 0:
                return None, self.SLIPPAGE_PCT * 100
            if side == "BUY":
                return round(ltp * (1 + self.SLIPPAGE_PCT), 2), self.SLIPPAGE_PCT * 100
            else:
                return round(ltp * (1 - self.SLIPPAGE_PCT), 2), self.SLIPPAGE_PCT * 100

    def _calculate_atr_targets(self, price, atr, side):
        # target_pct / stoploss_pct / max_target_pct / max_sl_pct are set in
        # __init__ from the user's Settings (per trading mode), not fixed
        # constants — see UserManager.get_user_risk_config().
        atr_pct = (atr / price) * 100 if price > 0 else 1.0
        if atr_pct > 2.0:
            if side == 'BUY':
                sl = round(price - atr * 1.0, 2)
                tgt = round(price + atr * 1.5, 2)
            else:
                sl = round(price + atr * 1.0, 2)
                tgt = round(price - atr * 1.5, 2)
        elif atr_pct < 0.5:
            # Low-volatility bars: tighten proportionally to the user's
            # configured target/SL (same 0.5x/0.6x ratio the old fixed
            # 0.5%/0.3% values implied against the old fixed 1.0%/0.5%).
            tight_tgt_pct = self.target_pct * 0.5
            tight_sl_pct = self.stoploss_pct * 0.6
            if side == 'BUY':
                tgt = round(price * (1.0 + tight_tgt_pct), 2)
                sl = round(price * (1.0 - tight_sl_pct), 2)
            else:
                tgt = round(price * (1.0 - tight_tgt_pct), 2)
                sl = round(price * (1.0 + tight_sl_pct), 2)
        else:
            if side == 'BUY':
                tgt = round(price * (1.0 + self.target_pct), 2)
                sl = round(price * (1.0 - self.stoploss_pct), 2)
            else:
                tgt = round(price * (1.0 - self.target_pct), 2)
                sl = round(price * (1.0 + self.stoploss_pct), 2)
        if side == 'BUY':
            tgt = min(tgt, round(price * (1.0 + self.max_target_pct), 2))
            sl = max(sl, round(price * (1.0 - self.max_sl_pct), 2))
        else:
            tgt = max(tgt, round(price * (1.0 - self.max_target_pct), 2))
            sl = min(sl, round(price * (1.0 + self.max_sl_pct), 2))
        if side == 'BUY':
            if sl >= price or tgt <= price or tgt <= sl:
                tgt = round(price * (1.0 + self.target_pct), 2)
                sl = round(price * (1.0 - self.stoploss_pct), 2)
        else:
            if tgt >= price or sl <= price or tgt >= sl:
                tgt = round(price * (1.0 - self.target_pct), 2)
                sl = round(price * (1.0 + self.stoploss_pct), 2)
        return tgt, sl
    
    def _check_entry_quality(self, df, ind, side, symbol, strategy_name=None):
        try:
            price = float(df['close'].iloc[-1])
            open_ = float(df['open'].iloc[-1])
            high_ = float(df['high'].iloc[-1])
            low_ = float(df['low'].iloc[-1])
            now = datetime.now()
            now_mins = now.hour * 60 + now.minute

            meta = AVAILABLE_STRATEGY_META.get(strategy_name, {})
            category = meta.get('category', 'default')
            skip_extension_checks = category in ('breakout', 'momentum')
            # skip_quality_checks: some strategies (e.g. a pure EMA20/EMA50
            # momentum-confirmation strategy) declare in their own
            # strategy_meta that NOTHING besides their own entry logic
            # should gate them — no volume-surge requirement, no
            # candle-reversal veto. Previously the volume-surge check and
            # candle-pattern veto below ran unconditionally for every
            # strategy regardless of category, which silently rejected
            # 100% of that strategy's signals (they'd log as fired, but
            # never actually open a position) even though its category
            # already exempted it from the VWAP/RSI/EMA50 checks further
            # down. Honor the flag before running ANY of the checks.
            if meta.get('skip_quality_checks'):
                return True, None

            def _iv(key, fallback=0.0):
                try:
                    v = float(ind[key].iloc[-1])
                    return fallback if (isinstance(v, float) and np.isnan(v)) else v
                except Exception:
                    return fallback
            ema50 = _iv('ema_50', price)
            rsi = _iv('rsi', 50.0)
            vwap = _iv('vwap', 0.0)
            vwap_u1 = _iv('vwap_upper1', price * 1.02)
            vwap_l1 = _iv('vwap_lower1', price * 0.98)
            roc5 = _iv('roc5', 0.0)
            htf_bull = _iv('htf_bull', 0.5)
            atr = _iv('atr', 0.0)
            avg_vol = float(df['volume'].iloc[-10:].mean()) if len(df) >= 10 else float(df['volume'].mean())
            cur_vol = float(df['volume'].iloc[-1])
            vol_mult = 1.0 if now_mins < 10*60 else 0.5 if now_mins < 13*60 else 0.4
            if cur_vol < avg_vol * vol_mult:
                return False, (f"Low volume {int(cur_vol):,} < "
                            f"{int(avg_vol * vol_mult):,} "
                            f"({vol_mult}x avg)")
            if cur_vol < avg_vol * self.MIN_VOL_SURGE:
                return False, (f"Volume surge insufficient: {cur_vol/avg_vol:.1f}x < {self.MIN_VOL_SURGE}x")

            def _candle_patterns(df_slice):
                cp = {}
                try:
                    n = len(df_slice) - 1
                    c = float(df_slice['close'].iloc[n])
                    o = float(df_slice['open'].iloc[n])
                    h = float(df_slice['high'].iloc[n])
                    l = float(df_slice['low'].iloc[n])
                    rng = max(h - l, 1e-9)
                    body = abs(c - o)
                    body_r = body / rng
                    uw_r = (h - max(c, o)) / rng
                    lw_r = (min(c, o) - l) / rng
                    bull = c >= o
                    bear = c < o
                    cp['DOJI'] = body_r < 0.10
                    cp['SPINNING_TOP'] = (0.10 <= body_r <= 0.30 and uw_r > 0.25 and lw_r > 0.25)
                    cp['HAMMER'] = (lw_r > 0.60 and body_r < 0.30 and uw_r < 0.15 and bull)
                    cp['INVERTED_HAMMER'] = (uw_r > 0.60 and body_r < 0.30 and lw_r < 0.15 and bull)
                    cp['SHOOTING_STAR'] = (uw_r > 0.60 and body_r < 0.30 and lw_r < 0.15 and bear)
                    cp['HANGING_MAN'] = (lw_r > 0.60 and body_r < 0.30 and uw_r < 0.15 and bear)
                    cp['BULL_MARUBOZU'] = (body_r > 0.85 and bull and uw_r < 0.08 and lw_r < 0.08)
                    cp['BEAR_MARUBOZU'] = (body_r > 0.85 and bear and uw_r < 0.08 and lw_r < 0.08)
                    if n >= 1:
                        pc = float(df_slice['close'].iloc[n-1])
                        po = float(df_slice['open'].iloc[n-1])
                        ph = float(df_slice['high'].iloc[n-1])
                        pl = float(df_slice['low'].iloc[n-1])
                        pbull = pc > po
                        pbear = pc < po
                        pb = abs(pc - po)
                        pm = (po + pc) / 2.0
                        cp['BULL_ENGULFING'] = (pbear and bull and o <= pc and c >= po and body >= pb)
                        cp['BEAR_ENGULFING'] = (pbull and bear and o >= pc and c <= po and body >= pb)
                        cp['PIERCING_LINE'] = (pbear and bull and o < pl and c > pm and c < po)
                        cp['DARK_CLOUD_COVER'] = (pbull and bear and o > ph and c < pm and c > pc)
                        cp['TWEEZER_TOP'] = (pbull and bear and abs(h - ph) / rng < 0.05)
                        cp['TWEEZER_BOTTOM'] = (pbear and bull and abs(l - pl) / rng < 0.05)
                    if n >= 2:
                        c2 = float(df_slice['close'].iloc[n-2])
                        o2 = float(df_slice['open'].iloc[n-2])
                        c1 = float(df_slice['close'].iloc[n-1])
                        o1 = float(df_slice['open'].iloc[n-1])
                        h1 = float(df_slice['high'].iloc[n-1])
                        l1 = float(df_slice['low'].iloc[n-1])
                        rng1 = max(h1 - l1, 1e-9)
                        body1 = abs(c1 - o1) / rng1
                        mid2 = (o2 + c2) / 2.0
                        cp['MORNING_STAR'] = (c2 < o2 and body1 < 0.30 and bull and c > mid2)
                        cp['EVENING_STAR'] = (c2 > o2 and body1 < 0.30 and bear and c < mid2)
                        cp['THREE_WHITE_SOLDIERS'] = (c2 > o2 and c1 > o1 and bull and c1 > c2 and c > c1 and body_r > 0.50)
                        cp['THREE_BLACK_CROWS'] = (c2 < o2 and c1 < o1 and bear and c1 < c2 and c < c1 and body_r > 0.50)
                except Exception as e:
                    logger.debug(f"_candle_patterns error: {e}")
                return cp
            cp = _candle_patterns(df)
            if side == 'BUY':
                bearish_veto = ['SHOOTING_STAR','EVENING_STAR','BEAR_ENGULFING','DARK_CLOUD_COVER','HANGING_MAN','THREE_BLACK_CROWS','BEAR_MARUBOZU','TWEEZER_TOP']
                triggered = [p for p in bearish_veto if cp.get(p)]
                if triggered:
                    return False, (f"Bearish candle pattern on trigger bar: {', '.join(triggered)}")
            if side == 'SELL':
                bullish_veto = ['HAMMER','MORNING_STAR','BULL_ENGULFING','PIERCING_LINE','INVERTED_HAMMER','THREE_WHITE_SOLDIERS','BULL_MARUBOZU','TWEEZER_BOTTOM']
                triggered = [p for p in bullish_veto if cp.get(p)]
                if triggered:
                    return False, (f"Bullish candle pattern on trigger bar: {', '.join(triggered)}")

            if not skip_extension_checks:
                if side == 'BUY':
                    if vwap_u1 > 0 and price > vwap_u1 and roc5 > 1.5:
                        return False, (f"Price extended above VWAP+1σ ({price:.2f} > {vwap_u1:.2f}) with ROC5={roc5:.1f}% — likely exhausted")
                    if atr > 0 and vwap > 0 and (price - vwap) > 2.0 * atr:
                        return False, (f"Price > 2×ATR above VWAP ({price:.2f} vs VWAP {vwap:.2f}, ATR {atr:.2f}) — late long entry")
                if side == 'SELL':
                    if vwap_l1 > 0 and price < vwap_l1 and roc5 < -1.5:
                        return False, (f"Price extended below VWAP-1σ ({price:.2f} < {vwap_l1:.2f}) with ROC5={roc5:.1f}% — likely exhausted")
                    if atr > 0 and vwap > 0 and (vwap - price) > 2.0 * atr:
                        return False, (f"Price > 2×ATR below VWAP ({price:.2f} vs VWAP {vwap:.2f}, ATR {atr:.2f}) — late short entry")
                if side == 'BUY' and rsi > 72 and htf_bull > 0.85:
                    return False, (f"Overbought — RSI {rsi:.1f} > 72 and HTF bull strength {htf_bull:.2f} > 0.85 (chasing a tired move)")
                if side == 'SELL' and rsi < 28 and htf_bull < 0.15:
                    return False, (f"Oversold — RSI {rsi:.1f} < 28 and HTF bull {htf_bull:.2f} < 0.15 (shorting into exhaustion)")
                if side == 'BUY' and ema50 > 0 and price < ema50 * 0.98:
                    return False, (f"BUY price {price:.2f} is >2% below EMA50 {ema50:.2f} — counter-trend long")
                if side == 'SELL' and ema50 > 0 and price > ema50 * 1.02:
                    return False, (f"SELL price {price:.2f} is >2% above EMA50 {ema50:.2f} — counter-trend short")
                if side == 'BUY' and rsi > 75:
                    return False, f"BUY into overbought RSI {rsi:.1f} > 75"
                if side == 'SELL' and rsi < 25:
                    return False, f"SELL into oversold RSI {rsi:.1f} < 25"
                if side == 'SELL' and vwap > 0 and price < vwap * 0.99:
                    return False, (f"SELL already below VWAP ({price:.2f} < {vwap:.2f}) — chasing the move down")

            return True, None
        except Exception as e:
            logger.error(f"Entry quality error {symbol}: {e}")
            return True, None
    
    def _open_position_nolock(self, symbol, side, price, reason='SIGNAL',
                             signal_score=None, strategy_name=None, atr=None,
                             df=None, ind=None, log_entry=None):
        def _block(msg):
            logger.info(f"BLOCKED {symbol}: {msg}")
            if log_entry is not None:
                entry = log_entry.copy()
                entry['status'] = 'REJECTED'
                entry['reason'] = f'POST-SIGNAL BLOCK: {msg}'
                self._add_signal_log(entry)
        can_trade, msg = self._check_circuit_breakers()
        if not can_trade:
            _block(f"Circuit breaker: {msg}")
            return None
        # CNC/Delivery is cash-only — you can only sell shares you already
        # hold, never open a fresh short. This is the single choke-point
        # every entry path (live signals, sector monitor, manual) goes
        # through, so blocking SELL here is what actually enforces the
        # restriction rather than relying on each caller to self-police.
        if side == 'SELL' and self.trading_mode == 'DELIVERY':
            _block("SELL blocked — CNC/Delivery mode does not support short selling")
            return None
        if len(self.data['positions']) >= self.MAX_OPEN_POS:
            _block(f"Max positions ({self.MAX_OPEN_POS}) already open")
            return None
        now = datetime.now()
        mins = now.hour * 60 + now.minute
        # The 9:15-14:00 trade slot and 14:30 cutoff exist only to guarantee
        # enough runway to square off an INTRADAY (MIS) position by 15:15.
        # CNC/Delivery has no forced square-off, so it may enter any time
        # during market hours (already gated to 9:18-15:30 by the caller in
        # _check_signal via MARKET_OPEN+NEW_ENTRY_WARMUP_MINUTES / MARKET_CLOSE).
        if self.trading_mode == 'INTRADAY':
            if mins >= self.NO_NEW_TRADES_AFTER:
                _block(f"After cutoff {now.strftime('%H:%M')} >= 14:30")
                return None
            if not _in_trade_slot(mins):
                _block(
                    f"Outside trade slot [{_slot_label(mins)}] — "
                    f"allowed 9:15–14:00"
                )
                return None
        if price < self.MIN_PRICE:
            _block(f"Price {price:.2f} below min {self.MIN_PRICE}")
            return None
        # NOTE: the gap-up/gap-down filter and the _check_entry_quality()
        # call (volume-surge, candle-pattern, VWAP/RSI/EMA50 extension
        # vetoes) that used to sit here have been removed entirely, per
        # requirement: a strategy signal should place a trade directly,
        # with no additional gates/panels layered on top by the engine.
        # The strategy file itself is now the ONLY thing deciding whether
        # an entry is valid. What remains above/below this point is not a
        # signal filter — it's order mechanics (circuit breaker, capital
        # sizing, trading-mode/session-window rules) needed to actually
        # place a real order, and stays regardless of which strategy fired.
        qty, margin_used, margin_pct, margin_source = self._smart_allocation_time_based(price, now, symbol=symbol, side=side, atr=atr)
        if qty == 0:
            wallet = self.data['wallet']
            usable = wallet * self.WALLET_USAGE_PCT
            est_margin = price * self.INTRADAY_MARGIN_PCT
            est_qty = int(usable // est_margin) if est_margin > 0 else 0
            if est_qty < 1:
                _block(f"Insufficient capital: wallet {wallet:.0f} x {self.WALLET_USAGE_PCT*100:.0f}% = "
                      f"{usable:.0f} usable, need {est_margin:.0f}/share")
            else:
                slot_m = now.hour * 60 + now.minute
                reduction = 0.25 if (12*60+30 < slot_m < 14*60+0) else (0.75 if (14*60+0 <= slot_m <= 15*60+30) else 1.0)
                _block(f"Qty reduced to 0 by slot factor {reduction*100:.0f}% [{_slot_label(slot_m)}]")
            return None
        if atr and atr > 0:
            target, stoploss = self._calculate_atr_targets(price, atr, side)
        else:
            if side == 'BUY':
                target = round(price * (1 + self.target_pct), 2)
                stoploss = round(price * (1 - self.stoploss_pct), 2)
            else:
                target = round(price * (1 - self.target_pct), 2)
                stoploss = round(price * (1 + self.stoploss_pct), 2)
        if side == 'BUY':
            target = max(target, price + self.MIN_ABSOLUTE_MOVE)
            stoploss = min(stoploss, price - self.MIN_ABSOLUTE_MOVE)
        else:
            target = min(target, price - self.MIN_ABSOLUTE_MOVE)
            stoploss = max(stoploss, price + self.MIN_ABSOLUTE_MOVE)
        fill_price, actual_slip_pct = self._get_l2_execution_price(symbol, side)
        if fill_price is None or fill_price <= 0:
            fill_price = round(
                price * (1.0 + self.SLIPPAGE_PCT) if side == 'BUY'
                else price * (1.0 - self.SLIPPAGE_PCT), 2
            )
            actual_slip_pct = self.SLIPPAGE_PCT * 100
        entry_chg = calc_zerodha_charges(fill_price, fill_price, qty)
        self._record_order(symbol, side, qty, fill_price, margin_used, reason, signal_score, entry_chg, strategy_name)
        old_w = self.data['wallet']
        self.data['wallet'] = round(old_w - margin_used, 2)
        pos = {
            'side': side,
            'qty': qty,
            'entry_price': round(fill_price, 2),
            'margin_used': margin_used,
            'full_value': price * qty,
            'entry_time': now.isoformat(),
            'entry_date': now.strftime('%Y-%m-%d'),
            'signal_score': signal_score,
            'strategy': strategy_name,
            'target': target,
            'stoploss': stoploss,
            'entry_atr': atr,
            'trail_activated': False,
            'peak_price': price,
            'margin_pct': margin_pct,
            'leverage': round(1 / margin_pct, 2) if margin_pct > 0 else 5.0,
            'margin_source': margin_source,
            'entry_slip_pct': actual_slip_pct,
        }
        self.data['positions'][symbol] = pos
        self.active_stock_orders[symbol] = now
        logger.info(f"OPENED {symbol} {side} {qty} @ {price:.2f}")
        logger.info(f"Target: {target:.2f} | SL: {stoploss:.2f} | Margin: {margin_used:.2f} "
                   f"({margin_pct*100:.1f}% = {round(1/margin_pct,2) if margin_pct>0 else 5}x)")
        self._save(force_backup=True)
        return pos
    
    def _close_position_nolock(self, symbol, exit_price=None, reason='SIGNAL'):
        pos = self.data['positions'].get(symbol)
        if not pos:
            return None
        qty = pos['qty']
        entry = pos['entry_price']
        side = pos['side']
        strategy_name = pos.get('strategy', 'UNKNOWN')
        if exit_price is None:
            exit_price = self._get_live_ltp(symbol) or entry
        if side == 'BUY':
            chg = calc_zerodha_charges(entry, exit_price, qty)
        else:
            chg = calc_zerodha_charges(exit_price, entry, qty)
        gross_pnl = chg['gross_pnl']
        net_pnl = chg['net_pnl']
        margin_used = pos.get('margin_used', entry * qty)
        if strategy_name not in self.strategy_performance:
            self.strategy_performance[strategy_name] = {
                'wins': 0, 'losses': 0, 'total_pnl': 0.0,
                'total_trades': 0, 'win_rate': 0.0
            }
        sp = self.strategy_performance[strategy_name]
        sp['total_trades'] += 1
        if net_pnl > 0:
            sp['wins'] += 1
        else:
            sp['losses'] += 1
        sp['total_pnl'] += net_pnl
        sp['win_rate'] = sp['wins'] / sp['total_trades'] if sp['total_trades'] > 0 else 0
        exit_side = 'SELL' if side == 'BUY' else 'BUY'
        self._record_order(symbol, exit_side, qty, exit_price, margin_used, reason,
                          pos.get('signal_score'), chg, strategy_name)
        trade = {
            'symbol': symbol,
            'side': side,
            'qty': qty,
            'entry_price': entry,
            'exit_price': round(exit_price, 2),
            'margin_used': margin_used,
            'full_value': entry * qty,
            'entry_time': pos['entry_time'],
            'exit_time': datetime.now().isoformat(),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'gross_pnl': gross_pnl,
            'pnl': net_pnl,
            'pnl_pct': round(net_pnl / (entry * qty) * 100, 2) if entry * qty > 0 else 0,
            'exit_reason': reason,
            'strategy': strategy_name,
            **{k: v for k, v in chg.items() if k in ['brokerage', 'stt', 'exchange_charge', 'gst', 'stamp_duty', 'total_charges']}
        }
        self.data['trades'].append(trade)
        self._update_daily_pnl(trade)
        self._update_daily_stats(net_pnl)
        old_w = self.data['wallet']
        self.data['wallet'] = round(old_w + margin_used + net_pnl, 2)
        del self.data['positions'][symbol]
        self.active_stock_orders.pop(symbol, None)
        self.last_exit_time[symbol] = datetime.now()
        logger.info(f"CLOSED {symbol} | Gross {gross_pnl:.2f} | Net {net_pnl:.2f}")
        self._save(force_backup=True)
        return trade
    
    def _update_daily_pnl(self, trade):
        d = trade['date']
        if d not in self.data['daily_pnl']:
            self.data['daily_pnl'][d] = {
                'realized': 0.0,
                'gross_realized': 0.0,
                'total_charges': 0.0,
                'trades': 0,
                'wins': 0,
                'losses': 0
            }
        dp = self.data['daily_pnl'][d]
        dp['realized'] = round(dp['realized'] + trade['pnl'], 2)
        dp['gross_realized'] = round(dp['gross_realized'] + trade['gross_pnl'], 2)
        dp['total_charges'] = round(dp['total_charges'] + trade['total_charges'], 2)
        dp['trades'] += 1
        if trade['pnl'] > 0:
            dp['wins'] += 1
        else:
            dp['losses'] += 1
    
    def _update_daily_stats(self, pnl):
        with self._lock:
            today = datetime.now().strftime('%Y-%m-%d')
            last_trade_date = self.data['trades'][-1]['date'] if self.data['trades'] else None
            if last_trade_date and last_trade_date != today:
                self.consecutive_losses = 0
            if today not in self.data['daily_pnl']:
                self.data['daily_pnl'][today] = {
                    'realized': 0.0, 'gross_realized': 0.0,
                    'total_charges': 0.0, 'trades': 0,
                    'wins': 0, 'losses': 0,
                    'peak_wallet': self.data['wallet']
                }
            stats = self.data['daily_pnl'][today]
            if self.data['wallet'] > stats.get('peak_wallet', 0):
                stats['peak_wallet'] = self.data['wallet']
            if pnl < 0:
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0
            self.last_trade_pnl = pnl
    
    def _record_order(self, symbol, side, qty, price, margin_used, reason,
                     signal_score, charges, strategy_name):
        order = {
            'order_id': f"PT{datetime.now().strftime('%H%M%S%f')[:12]}",
            'symbol': symbol,
            'side': side,
            'qty': qty,
            'price': round(price, 2),
            'margin_used': round(margin_used, 2),
            'value': round(price * qty, 2),
            'time': datetime.now().isoformat(),
            'date': datetime.now().strftime('%Y-%m-%d'),
            'reason': reason,
            'signal_score': signal_score,
            'strategy': strategy_name,
            'status': 'EXECUTED',
            'brokerage': round(charges.get('brokerage', 0), 2),
            'stt': round(charges.get('stt', 0), 2),
            'total_charges': round(charges.get('total_charges', 0), 2),
            'is_intraday': True
        }
        self.data['orders'].append(order)
        return order
    
    def force_exit(self, symbol, reason='MANUAL_EXIT'):
        ltp = self._get_live_ltp(symbol)
        with self._lock:
            if symbol not in self.data['positions']:
                return None
            return self._close_position_nolock(symbol, exit_price=ltp, reason=reason)
    
    def _squareoff_all(self):
        logger.info("Squaring off all positions...")
        with self._lock:
            for sym in list(self.data['positions'].keys()):
                self._close_position_nolock(sym, reason='INTRADAY_SQ_OFF')
    
    def get_signal_logs(self, date=None, status=None, limit=100):
        with self._lock:
            logs = list(self._signal_logs)
            if date:
                logs = [l for l in logs if l.get('date') == date]
            if status:
                if status == 'REJECTED':
                    logs = [l for l in logs if l.get('status') in ['REJECTED', 'COOLDOWN', 'BLOCKED_OTHER_POS']]
                else:
                    logs = [l for l in logs if l.get('status') == status]
            return logs[:limit]
    
    def _add_signal_log(self, log_entry):
        log_entry['timestamp'] = datetime.now().isoformat()
        self._signal_logs.appendleft(log_entry)
        with self._lock:
            self.data['signal_logs'] = list(self._signal_logs)[:5000]
            self._save()
        if log_entry.get('status') in ['BUY_SIGNAL', 'SELL_SIGNAL']:
            logger.info(f"SIGNAL {log_entry['symbol']} {log_entry['status']} {log_entry.get('reason', '')}")
    
    def _should_use_strategy(self, name):
        if name not in self.strategy_performance:
            return True
        sp = self.strategy_performance[name]
        total = sp.get('total_trades', 0)
        if total < self.STRATEGY_MIN_TRADES:
            return True
        return sp.get('win_rate', 0) >= self.STRATEGY_MIN_WIN_RATE
    
    def _get_sector_bias(self, symbol):
        now = time.time()
        for sector, stocks in SECTOR_MAP.items():
            if symbol in stocks:
                bias = self._sector_bias_cache.get(sector)
                if bias and (now - bias.get('updated', 0)) < self.SECTOR_BIAS_TTL:
                    return bias['direction'], sector, bias
                return None, sector, None
        return None, None, None

    

    def _check_signal(self, symbol, kite, prefetched_ltp=None):
        if symbol not in SYMBOL_MAP:
            self._log_diag(
                f"{symbol}:not_in_map",
                {
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "date": datetime.now().strftime("%Y-%m-%d"),
                    "symbol": symbol,
                    "ltp": round(prefetched_ltp, 2) if prefetched_ltp is not None else 0,
                    "status": "REJECTED",
                    "reason": "Symbol not found in instrument cache (delisted/renamed/typo?)",
                },
            )
            return
        try:
            now = datetime.now()
            mins = now.hour * 60 + now.minute

            with self._lock:
                _has_open_pos = symbol in self.data.get("positions", {})

            if not _has_open_pos:
                if self.MARKET_OPEN <= mins < self.MARKET_OPEN + self.NEW_ENTRY_WARMUP_MINUTES:
                    return
                if mins >= self.MARKET_CLOSE:
                    return
            elif mins >= self.MARKET_CLOSE:
                return

            _current_slot_ok = _in_trade_slot(mins)
            _current_slot_lbl = _slot_label(mins)

            min_bars_needed = _get_strategy_min_bars(self.strategies_dict)
            strat_timeframe = _get_strategy_timeframe(self.strategies_dict)
            strat_lookback_days = _lookback_days(strat_timeframe, min_bars_needed)

            _hist_limiter.wait(f"{strat_timeframe} {symbol}")
            data5 = kite.historical_data(
                SYMBOL_MAP[symbol]["token"],
                now - timedelta(days=strat_lookback_days),
                now,
                strat_timeframe,
            )
            if not data5 or len(data5) < min_bars_needed:
                self._log_diag(
                    f"{symbol}:insufficient_data",
                    {
                        "time": now.strftime("%H:%M:%S"),
                        "date": now.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "ltp": round(prefetched_ltp, 2) if prefetched_ltp is not None else 0,
                        "status": "REJECTED",
                        "reason": (
                            f"Insufficient historical data "
                            f"({len(data5) if data5 else 0} bars, need ≥{min_bars_needed})"
                        ),
                    },
                )
                return

            df = pd.DataFrame(data5)
            ind = Indicators.calculate_all(df)

            ltp_initial = (
                prefetched_ltp
                if prefetched_ltp is not None
                else float(df["close"].iloc[-1])
            )

            if ltp_initial < self.MIN_PRICE:
                self._log_diag(
                    f"{symbol}:min_price",
                    {
                        "time": now.strftime("%H:%M:%S"),
                        "date": now.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "ltp": round(ltp_initial, 2),
                        "status": "REJECTED",
                        "reason": (
                            f"Price ₹{ltp_initial:.2f} below minimum "
                            f"₹{self.MIN_PRICE:.0f} for trading"
                        ),
                    },
                )
                return

            avg_vol = float(df["volume"].iloc[-20:].mean())
            cur_vol = int(df["volume"].iloc[-1])
            now_mins = now.hour * 60 + now.minute
            # NOTE: the pre-strategy low-volume rejection that used to sit
            # here (returning before any strategy function was even
            # called) has been removed — it silently killed every signal
            # regardless of what the strategy itself actually requires.
            # Volume/quality filtering, if a strategy wants it, belongs
            # inside that strategy's own file, not as a blanket engine gate.

            # NOTE: pin-direction restriction removed along with the
            # Monitored/pinned feature — every symbol in the scan universe
            # (NIFTY 200) is now checked for both BUY and SELL signals.
            allow_buy = True
            allow_sell = True

            df_w = df.iloc[-min_bars_needed:].reset_index(drop=True)
            ind_w = ind.iloc[-min_bars_needed:].reset_index(drop=True)

            strat5_b, strat5_s, _ = _strat_votes(df_w, ind_w, self.strategies_dict, self.strategy_performance)
            st5_total = strat5_b + strat5_s
            s5_buy_pct = strat5_b / st5_total * 100 if st5_total > 0 else 50.0
            s5_sel_pct = strat5_s / st5_total * 100 if st5_total > 0 else 50.0

            htf15_buy_pct = 50.0
            htf15_sel_pct = 50.0
            htf15_ok_buy = False
            htf15_ok_sell = False

            try:
                _hist_limiter.wait(f"15min signal {symbol}")
                data15 = kite.historical_data(
                    SYMBOL_MAP[symbol]["token"],
                    now - timedelta(days=14),
                    now,
                    "15minute",
                )
                if data15 and len(data15) >= min_bars_needed:
                    df15 = pd.DataFrame(data15)
                    ind15 = Indicators.calculate_all(df15)
                    df15_w = df15.iloc[-min_bars_needed:].reset_index(drop=True)
                    ind15_w = ind15.iloc[-min_bars_needed:].reset_index(drop=True)
                    b15, s15, _ = _strat_votes(df15_w, ind15_w, self.strategies_dict, self.strategy_performance)
                    t15 = b15 + s15
                    htf15_buy_pct = b15 / t15 * 100 if t15 > 0 else 50.0
                    htf15_sel_pct = s15 / t15 * 100 if t15 > 0 else 50.0
                    htf15_ok_buy = htf15_buy_pct >= self.MIN_VOTE_PCT
                    htf15_ok_sell = htf15_sel_pct >= self.MIN_VOTE_PCT
            except Exception as e:
                logger.debug(f"15-min data error {symbol}: {e}")

            htf_bull = (
                float(ind["htf_bull"].iloc[-1])
                if "htf_bull" in ind.columns
                else 0.5
            )

            sector_bias_log = ""

            buy_score = s5_buy_pct
            sell_score = s5_sel_pct
            htf15_conflict_buy = htf15_ok_sell and not htf15_ok_buy
            htf15_conflict_sell = htf15_ok_buy and not htf15_ok_sell
            effective_buy_min = self.MIN_VOTE_PCT
            effective_sell_min = self.MIN_VOTE_PCT

            # NOTE: previously this required a strategy's votes to clear a
            # percentage-of-total-votes threshold (MIN_VOTE_PCT) — a
            # "panel" gate on top of the strategy's own signal. Removed:
            # a strategy triggering (strat5_b/s >= 1.0) IS the signal,
            # full stop.
            buy_ok = allow_buy and strat5_b >= 1.0
            sell_ok = (
                allow_sell
                and strat5_s >= 1.0
                and self.trading_mode != 'DELIVERY'
            )

            if buy_ok and sell_ok:
                if sell_score > buy_score:
                    buy_ok = False
                else:
                    sell_ok = False

            all_triggered = []
            for name, func in self.strategies_dict.items():
                try:
                    if func(df_w, ind_w):
                        all_triggered.append(name)
                except Exception:
                    continue

            direction_triggered = [
                n for n in all_triggered
                if (lambda _dir: (
                    (allow_buy and _dir == 'BUY')
                    or (allow_sell and _dir == 'SELL')
                    or _dir == 'BOTH'
                ))(AVAILABLE_STRATEGY_META.get(n, {}).get('direction', 'BOTH'))
            ]

            best_strategy = None
            best_sc = -1
            for name in direction_triggered:
                sc = 70 + len(direction_triggered) * 5
                if self._should_use_strategy(name):
                    sc += 10
                if sc > best_sc:
                    best_sc = sc
                    best_strategy = name

            if not best_strategy and (buy_ok or sell_ok):
                best_strategy = "VOTE_SIGNAL"

            atr = float(ind["atr"].iloc[-1]) if "atr" in ind.columns else None
            nifty_data = get_nifty_data(kite)

            if nifty_data:
                market_trend = (
                    "BULLISH" if nifty_data["change"] > 0.3
                    else "BEARISH" if nifty_data["change"] < -0.3
                    else "NEUTRAL"
                )
                market_regime = "TRENDING" if nifty_data["adx"] > 25 else "RANGING"
            else:
                market_trend = "NEUTRAL"
                market_regime = "UNKNOWN"

            # NOTE: market_trend/market_regime are still computed and logged
            # for visibility, but no longer used to block a signal — the
            # "skip breakout strategies while market is RANGING" gate has
            # been removed. A strategy that triggers places a trade.
            skip_breakout = False

            # Ask each strategy function in the user's selected strategy file
            # for its own live diagnostic data (EMA values, thresholds,
            # whatever it wants to expose) via the optional
            # AVAILABLE_STRATEGY_DIAGNOSTICS registry — see strategies.py.
            # Computed every cycle (regardless of trigger/status) purely from
            # data already fetched (df_w/ind_w), no extra API calls. This is
            # what drives the Signal Log UI's dynamic per-strategy column —
            # a strategy with no diagnostics function registered simply
            # contributes nothing here.
            strategy_data = {}
            for _sd_name in self.strategies_dict:
                _diag_fn = AVAILABLE_STRATEGY_DIAGNOSTICS.get(_sd_name)
                if not _diag_fn:
                    continue
                try:
                    _diag = _diag_fn(df_w, ind_w)
                    if _diag:
                        strategy_data[_sd_name] = _diag
                except Exception as e:
                    logger.debug(f"Strategy diagnostics error {_sd_name} {symbol}: {e}")

            log_entry = {
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "ltp": round(ltp_initial, 2),
                "volume": int(df["volume"].iloc[-1]),
                "avg_volume": int(avg_vol),
                "buy_score": round(buy_score, 1),
                "sell_score": round(sell_score, 1),
                "hard_buy": buy_ok,
                "hard_sell": sell_ok,
                "strategies": direction_triggered[:5],
                "all_strategies": all_triggered[:8],
                "strategy_data": strategy_data,
                "best_strategy": best_strategy,
                "sector_bias": sector_bias_log,
                "market_trend": market_trend,
                "market_regime": market_regime,
                "strategy_mode": f"NATIVE ({self.strategy_name})",
            }

            try:
                _cp_now, _, _ = detect_candle_patterns(df, len(df) - 1)
                log_entry["candle_patterns"] = [k for k, v in _cp_now.items() if v][:6]
            except Exception:
                log_entry["candle_patterns"] = []

            with self._lock:
                pos = self.data["positions"].get(symbol)

                if pos:
                    log_entry["status"] = "IN_POSITION"
                    log_entry["pos_side"] = pos["side"]
                    log_entry["pos_entry"] = pos["entry_price"]
                    log_entry["pos_qty"] = pos["qty"]
                    self._add_signal_log(log_entry)

                    fresh_ltp = (
                        prefetched_ltp
                        if prefetched_ltp is not None
                        else (self._get_live_ltp(symbol) or ltp_initial)
                    )

                    try:
                        held_mins = (
                            (datetime.now() - datetime.fromisoformat(pos["entry_time"]))
                            .total_seconds() / 60
                        )
                    except Exception:
                        held_mins = 0

                    move_pct = (
                        ((fresh_ltp - pos["entry_price"]) / pos["entry_price"] * 100)
                        if pos["side"] == "BUY"
                        else (
                            (pos["entry_price"] - fresh_ltp) / pos["entry_price"] * 100
                        )
                    )

                    hold_locked = False
                    if self.trading_mode == 'DELIVERY' and self.min_hold_days > 0:
                        held_days = held_mins / (60 * 24)
                        hold_locked = held_days < self.min_hold_days
                        log_entry["hold_days_left"] = round(self.min_hold_days - held_days, 2)

                    if hold_locked:
                        exit_price, reason = None, None
                    else:
                        exit_price, reason = self._check_reversal_exit(symbol, pos, df, ind)
                        if not exit_price:
                            self._update_trailing_stop(pos, fresh_ltp, move_pct, mins)
                            exit_price, reason = self._check_stop_loss_target(
                                symbol, pos, df, fresh_ltp
                            )

                    if exit_price:
                        if symbol in self.data["positions"]:
                            el = log_entry.copy()
                            el.update({"status": f"EXIT_{reason}", "exit_price": exit_price})
                            self._add_signal_log(el)
                            self._close_position_nolock(
                                symbol, exit_price=exit_price, reason=reason
                            )
                            self._save()
                        return

                    

                    self._save()
                    return

                if self.trading_mode == 'INTRADAY':
                    if mins >= self.NO_NEW_TRADES_AFTER:
                        log_entry["status"] = "REJECTED"
                        log_entry["reason"] = (
                            f"After cutoff {now.strftime('%H:%M')} (cutoff=14:30)"
                        )
                        self._add_signal_log(log_entry)
                        return

                    if not _current_slot_ok:
                        log_entry["status"] = "REJECTED"
                        log_entry["reason"] = (
                            f"Outside trade slot — {_current_slot_lbl} "
                            f"| Allowed: 9:15–14:00"
                        )
                        self._add_signal_log(log_entry)
                        return

                if symbol in self.last_exit_time:
                    ts = (now - self.last_exit_time[symbol]).total_seconds() / 60
                    if ts < self.COOLDOWN_MINUTES:
                        log_entry["status"] = "COOLDOWN"
                        log_entry["reason"] = (
                            f"Cooldown {ts:.1f}m / {self.COOLDOWN_MINUTES}m remaining"
                        )
                        self._add_signal_log(log_entry)
                        return

                if self.data["positions"]:
                    other = list(self.data["positions"].keys())[0]
                    log_entry["status"] = "BLOCKED_OTHER_POS"
                    log_entry["reason"] = f"Already in position: {other}"
                    self._add_signal_log(log_entry)
                    return

                if skip_breakout:
                    log_entry["status"] = "REJECTED"
                    log_entry["reason"] = (
                        f"BREAKOUT strategy skipped — market is {market_regime}"
                    )
                    self._add_signal_log(log_entry)
                    return

                # NOTE: the correlation gate (blocking a BUY/SELL on a
                # HEAVYWEIGHTS symbol that "fights" the NIFTY trend) has
                # been removed — a strategy signal is no longer second-
                # guessed against index trend.



                if buy_ok:
                    _quote_limiter.wait(f"order_ltp BUY {symbol}")
                    ltp_for_order = kite.ltp([f"NSE:{symbol}"])
                    ltp_for_order = (
                        float(ltp_for_order[f"NSE:{symbol}"]["last_price"])
                        if ltp_for_order and f"NSE:{symbol}" in ltp_for_order
                        else ltp_initial
                    )
                    log_entry["status"] = "BUY_SIGNAL"
                    log_entry["ltp"] = round(ltp_for_order, 2)
                    log_entry["reason"] = (
                        f"Strategy: {best_strategy} (Score: {round(buy_score,1)}) | "
                        f"5m Vote: {round(s5_buy_pct,1)}% | "
                        f"15m Vote: {round(htf15_buy_pct,1)}% | "
                        f"HTF: {round(htf_bull,3)} | "
                        f"Slot: {_current_slot_lbl}"
                    )
                    self._add_signal_log(log_entry)
                    result = self._open_position_nolock(
                        symbol, "BUY", ltp_for_order, "ALGO_BUY",
                        round(buy_score, 1), best_strategy, atr, df, ind, log_entry,
                    )
                    if result is None:
                        for lg in self._signal_logs:
                            if (
                                lg.get("symbol") == symbol
                                and lg.get("status") == "BUY_SIGNAL"
                                and lg.get("time") == log_entry.get("time")
                            ):
                                lg["status"] = "BUY_NO_FILL"
                                break
                        self._save()

                elif sell_ok:
                    _quote_limiter.wait(f"order_ltp SELL {symbol}")
                    ltp_for_order = kite.ltp([f"NSE:{symbol}"])
                    ltp_for_order = (
                        float(ltp_for_order[f"NSE:{symbol}"]["last_price"])
                        if ltp_for_order and f"NSE:{symbol}" in ltp_for_order
                        else ltp_initial
                    )
                    log_entry["status"] = "SELL_SIGNAL"
                    log_entry["ltp"] = round(ltp_for_order, 2)
                    log_entry["reason"] = (
                        f"Strategy: {best_strategy} (Score: {round(sell_score,1)}) | "
                        f"5m Vote: {round(s5_sel_pct,1)}% | "
                        f"15m Vote: {round(htf15_sel_pct,1)}% | "
                        f"HTF: {round(htf_bull,3)} | "
                        f"Slot: {_current_slot_lbl}"
                    )
                    self._add_signal_log(log_entry)
                    result = self._open_position_nolock(
                        symbol, "SELL", ltp_for_order, "ALGO_SELL",
                        round(sell_score, 1), best_strategy, atr, df, ind, log_entry,
                    )
                    if result is None:
                        for lg in self._signal_logs:
                            if (
                                lg.get("symbol") == symbol
                                and lg.get("status") == "SELL_SIGNAL"
                                and lg.get("time") == log_entry.get("time")
                            ):
                                lg["status"] = "SELL_NO_FILL"
                                break
                        self._save()

                else:
                    reasons = []
                    leaning_buy = buy_score >= sell_score
                    check_dir = "BUY" if leaning_buy else "SELL"
                    if check_dir == "BUY":
                        if strat5_b < 1.0:
                            reasons.append(f"No {self.strategy_name} BUY strategy triggered")
                    else:
                        if strat5_s < 1.0:
                            reasons.append(f"No {self.strategy_name} SELL strategy triggered")

                    log_entry["status"] = "REJECTED"
                    log_entry["reason"] = (
                        " | ".join(reasons[:3]) if reasons else "No clear signal"
                    )
                    self._add_signal_log(log_entry)

                self._save()

        except Exception as e:
            logger.error(f"Signal check error {symbol}: {e}")
            traceback.print_exc()
            try:
                self._add_signal_log(
                    {
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "date": datetime.now().strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "status": "ERROR",
                        "reason": str(e)[:150],
                    }
                )
            except Exception:
                pass
    
    def _monitor_loop(self):
        logger.info("Monitor started")
        squaredoff_today = None
        check_counter = 0
        last_health = datetime.now()
        error_count = 0
 
        while self._running:
            try:
                now = datetime.now()
                today = now.date()
                mins = now.hour * 60 + now.minute
                is_wday = now.weekday() < 5
                self._last_heartbeat = now

                if (is_wday and mins >= self.SQUARE_OFF_TIME and squaredoff_today != today
                        and self.trading_mode == 'INTRADAY'):
                    if self.data["positions"]:
                        self._squareoff_all()
                    squaredoff_today = today

                if is_wday and self.MARKET_OPEN <= mins <= self.MARKET_CLOSE:
                    with self._lock:
                        positions = list(self.data.get("positions", {}).keys())
                    universe = [s for s in NIFTY200_SYMBOLS if s in SYMBOL_MAP]
                    all_syms = list(set(universe + positions))
                    if all_syms:
                        try:
                            batch_prices = self._get_batch_ltp(all_syms)
                        except Exception as e:
                            logger.error(f"Batch LTP error in monitor: {e}")
                            batch_prices = {}
                    else:
                        batch_prices = {}
                    for sym in all_syms:
                        prefetched_ltp = batch_prices.get(sym)
                        self._check_signal(sym, self._kite, prefetched_ltp=prefetched_ltp)
                    check_counter += 1
                    if check_counter % 30 == 0:
                        logger.info(f"Monitoring {len(universe)} NIFTY 200 stocks + {len(positions)} positions  |  batch_ltp covered {len(all_syms)} symbols")
                    error_count = 0

                if (now - last_health).total_seconds() > 60:
                    last_health = now
                    logger.debug(f"Health OK {now.strftime('%H:%M:%S')}")

                time.sleep(1)
            except Exception as e:
                error_count += 1
                logger.error(f"Monitor loop error: {e}")
                traceback.print_exc()
                if error_count > 10:
                    logger.critical("Too many errors, restarting monitor thread")
                    self._running = False
                    break
                time.sleep(5)
    
    def start(self, kite):
        self._kite = kite
        if not self._running:
            self._running = True
            self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self._monitor_thread.start()
            self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
            self._health_thread.start()
            self._fast_exit_thread = threading.Thread(target=self._fast_exit_loop, daemon=True)
            self._fast_exit_thread.start()
            logger.info("PaperTradingEngine started")
    
    def _health_loop(self):
        while self._running:
            try:
                time.sleep(30)
                if not self._monitor_thread or not self._monitor_thread.is_alive():
                    logger.warning("Monitor thread dead, restarting...")
                    self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
                    self._monitor_thread.start()
            except Exception as e:
                logger.error(f"Health loop error: {e}")
    
    def stop(self):
        self._running = False
        logger.info("PaperTradingEngine stopped")
    
    def get_unrealized_pnl(self):
        with self._lock:
            snapshot = [(s, p.copy()) for s, p in self.data['positions'].items()]
        total = 0.0
        detail = {}
        for sym, pos in snapshot:
            ltp = self._get_live_ltp(sym) or pos['entry_price']
            if pos['side'] == 'BUY':
                chg = calc_zerodha_charges(pos['entry_price'], ltp, pos['qty'])
            else:
                chg = calc_zerodha_charges(ltp, pos['entry_price'], pos['qty'])
            full_val = pos['entry_price'] * pos['qty']
            detail[sym] = {
                'ltp': round(ltp, 2),
                'gross_upnl': round(chg['gross_pnl'], 2),
                'upnl': round(chg['net_pnl'], 2),
                'pct': round(chg['net_pnl'] / full_val * 100, 2) if full_val > 0 else 0,
                'target': pos.get('target'),
                'stoploss': pos.get('stoploss'),
                'margin_used': pos.get('margin_used'),
                'est_charges': round(chg['total_charges'], 2),
                'pos': {
                    'entry_price': pos['entry_price'],
                    'side': pos['side'],
                    'qty': pos['qty'],
                    'strategy': pos.get('strategy', '—'),
                    'entry_time': pos.get('entry_time', ''),
                    'signal_score': pos.get('signal_score'),
                    'margin_pct': pos.get('margin_pct'),
                    'leverage': pos.get('leverage')
                }
            }
            total += chg['net_pnl']
        return round(total, 2), detail
    
    def summary(self):
        with self._lock:
            wallet = self.data['wallet']
            trades = list(self.data['trades'])
            daily_pnl = self.data['daily_pnl'].copy()
            circuit_breaker = self.data.get('circuit_breaker', {}).copy()
            used_margin = sum(p.get('margin_used', p['entry_price'] * p['qty']) 
                             for p in self.data['positions'].values())
            orders = list(self.data.get('orders', []))
        realized = sum(t['pnl'] for t in trades)
        wins = [t for t in trades if t['pnl'] > 0]
        losses = [t for t in trades if t['pnl'] <= 0]
        total_charges_paid = sum(t.get('total_charges', 0) for t in trades)
        gross_realized = sum(t.get('gross_pnl', t['pnl']) for t in trades)
        unrealized, pos_detail = self.get_unrealized_pnl()
        total_capital = wallet + used_margin
        today = datetime.now().strftime('%Y-%m-%d')
        today_stats = daily_pnl.get(today, {})
        latest_order = orders[-1] if orders else None
        return {
            'wallet': wallet,
            'available': max(0.0, wallet),
            'used_margin': used_margin,
            'used_percent': round(used_margin / total_capital * 100, 1) if total_capital > 0 else 0,
            'realized_pnl': round(realized, 2),
            'gross_realized_pnl': round(gross_realized, 2),
            'total_charges_paid': round(total_charges_paid, 2),
            'unrealized_pnl': unrealized,
            'total_pnl': round(realized + unrealized, 2),
            'total_trades': len(trades),
            'win_trades': len(wins),
            'loss_trades': len(losses),
            'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'open_positions': len(pos_detail),
            'universe_count': len(NIFTY200_SYMBOLS),
            'positions': pos_detail,
            'daily_pnl': daily_pnl,
            'today_pnl': today_stats.get('realized', 0),
            'today_trades': today_stats.get('trades', 0),
            'circuit_breaker': circuit_breaker,
            'consecutive_losses': self.consecutive_losses,
            'order_count': len(orders),
            'latest_order': latest_order,
            'target_pct': round(self.target_pct * 100, 2),
            'sl_pct': round(self.stoploss_pct * 100, 2),
            'mode': self.trading_mode,
            'strategy_name': self.strategy_name,
        }
    
    
# ==================== SECTOR MONITOR ====================
class SectorMonitor:
    def __init__(self, paper_engine):
        self.paper_engine = paper_engine
        self.sector_map = SECTOR_MAP
        self._running = False
        self._thread = None
        self._last_signal_time = {}
        self._cooldown_seconds = 120
        logger.info("SectorMonitor initialized")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("SectorMonitor started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("SectorMonitor stopped")

    def _monitor_loop(self):
        while self._running:
            try:
                now = datetime.now()
                mins = now.hour * 60 + now.minute
                if not (9*60+15 <= mins <= 15*60+30):
                    time.sleep(30)
                    continue

                if not _in_trade_slot(mins):
                    logger.debug(f"SectorMonitor: outside trade slot [{_slot_label(mins)}] — skipping")
                    time.sleep(30)
                    continue

                logger.debug("SectorMonitor scanning sectors...")
                for sector, stocks in self.sector_map.items():
                    if not stocks:
                        continue
                    signal = self._analyze_sector(sector, stocks)
                    if signal:
                        last = self._last_signal_time.get(sector)
                        if last and (now - last).total_seconds() < self._cooldown_seconds:
                            continue
                        self._last_signal_time[sector] = now
                        self._execute_sector_trade(sector, signal)
                time.sleep(60)
            except Exception as e:
                logger.error(f"SectorMonitor loop error: {e}")
                time.sleep(10)

    def _analyze_sector(self, sector, stocks):
        try:
            sample = stocks[:10]
            batch_prices = self.paper_engine._get_batch_ltp(sample)
            if not batch_prices:
                return None

            rep_stock = sample[0]
            token = SYMBOL_MAP.get(rep_stock, {}).get("token")
            if not token:
                return None

            end = datetime.now()
            start = end - timedelta(hours=1)
            _hist_limiter.wait(f"sector {sector}")
            data = self.paper_engine._kite.historical_data(token, start, end, "3minute")
            if not data or len(data) < 5:
                return None

            df = pd.DataFrame(data)
            close = df['close'].iloc[-1]
            prev_close = df['close'].iloc[-2]
            change_5min = (close - prev_close) / prev_close * 100 if prev_close else 0

            cur_vol = df['volume'].iloc[-1]
            avg_vol = df['volume'].iloc[-5:].mean()
            vol_ratio = cur_vol / avg_vol if avg_vol > 0 else 1.0

            try:
                ind = Indicators.calculate_all(df)
                rsi = ind['rsi'].iloc[-1]
            except:
                rsi = 50.0

            direction = None
            if change_5min > 0.8 and vol_ratio > 1.2 and rsi > 60:
                direction = "BUY"
            elif change_5min < -0.8 and vol_ratio > 1.2 and rsi < 40:
                direction = "SELL"

            if direction:
                self.paper_engine._sector_bias_cache[sector] = {
                    'direction': direction,
                    'change_5min': round(change_5min, 3),
                    'vol_ratio': round(float(vol_ratio), 3),
                    'rsi': round(float(rsi), 1),
                    'updated': time.time(),
                }
                logger.debug(
                    f"Sector bias [{sector}]: {direction}"
                    f"  Δ5m={change_5min:.2f}%  vol={vol_ratio:.2f}x  RSI={rsi:.1f}"
                )
            else:
                self.paper_engine._sector_bias_cache.pop(sector, None)
            return direction

        except Exception as e:
            logger.error(f"Sector analysis error {sector}: {e}")
            return None

    def _execute_sector_trade(self, sector, direction):
        stocks = self.sector_map.get(sector, [])
        if not stocks:
            return

        best_stock = None
        for sym in stocks:
            if sym in self.paper_engine.data.get('positions', {}):
                continue
            ltp = self.paper_engine._get_batch_ltp([sym]).get(sym)
            if ltp and ltp > 50:
                best_stock = sym
                break

        if not best_stock:
            logger.info(f"Sector {sector} {direction} - no available stock to trade")
            return

        token = SYMBOL_MAP[best_stock]['token']
        try:
            _hist_limiter.wait(f"sector_trade {best_stock}")
            data = self.paper_engine._kite.historical_data(token, datetime.now() - timedelta(days=3), datetime.now(), "3minute")
            if not data or len(data) < 80:
                logger.warning(f"Sector trade: insufficient data for {best_stock}")
                return
            df = pd.DataFrame(data)
            ind = Indicators.calculate_all(df)
            price = self.paper_engine._get_batch_ltp([best_stock]).get(best_stock) or float(df['close'].iloc[-1])

            atr = float(ind['atr'].iloc[-1]) if 'atr' in ind.columns else None

            with self.paper_engine._lock:
                pos = self.paper_engine._open_position_nolock(
                    best_stock, direction, price, reason='SECTOR_SIGNAL',
                    signal_score=70, strategy_name=f"SECTOR_{sector}", atr=atr,
                    df=df, ind=ind, log_entry=None
                )
            if pos:
                logger.info(f"Sector {sector} triggered {direction} on {best_stock} @ {price:.2f}")
            else:
                logger.info(f"Sector {sector} {direction} on {best_stock} rejected by entry checks")
        except Exception as e:
            logger.error(f"Error executing sector trade {best_stock}: {e}")

# ==================== BACKTEST ENGINE ====================
class BacktestEngine:
    def __init__(self, strategies_dict, trading_mode='INTRADAY', target_pct=None, stoploss_pct=None,
                 min_hold_days=None, strategy_performance=None):
        self.strategies_dict = strategies_dict
        self.trading_mode = trading_mode if trading_mode in ('INTRADAY', 'DELIVERY') else 'INTRADAY'
        self.results = None
        # Snapshot of the live paper-trading engine's per-strategy win-rate
        # stats (PaperTradingEngine.strategy_performance) at the moment the
        # backtest was launched. Passed straight through to _strat_votes()
        # below so a strategy's vote weight is scaled the same way live does
        # it (see _strategy_perf_weight) instead of always weighting 1.0 —
        # previously backtest always used the unweighted default regardless
        # of what live had actually learned, which was one more way the two
        # engines could disagree on which side a signal favored.
        self.strategy_performance = strategy_performance or {}
        # Minimum number of *calendar* days a DELIVERY/CNC position must be
        # held before TARGET/STOP_LOSS can close it. Real CNC positions
        # settle T+1 and are meant to be swing/investment holds, not same-
        # session flips — without this the backtest was happily opening and
        # closing the same symbol multiple times a day even in "Delivery"
        # mode, which isn't how a real CNC account behaves. Controlled from
        # the UI (Backtest tab); ignored for INTRADAY, which has its own
        # 15:15 same-day square-off logic instead.
        try:
            mhd = int(min_hold_days) if min_hold_days is not None else 1
        except (TypeError, ValueError):
            mhd = 1
        self.min_hold_days = max(0, min(30, mhd)) if self.trading_mode == 'DELIVERY' else 0
        # Target/SL come from the caller (UI-configured, per trading mode —
        # see UserManager.get_user_risk_config / /api/backtest/run) rather
        # than being fixed here. Fall back to sane per-mode defaults only if
        # the caller didn't pass anything.
        if target_pct is not None:
            self.target_pct = float(target_pct)
        else:
            self.target_pct = 0.03 if self.trading_mode == 'DELIVERY' else 0.010
        if stoploss_pct is not None:
            self.stoploss_pct = float(stoploss_pct)
        else:
            self.stoploss_pct = 0.015 if self.trading_mode == 'DELIVERY' else 0.005
        # Hard ceiling above the configured target/SL, mirroring
        # PaperTradingEngine.__init__'s max_target_pct/max_sl_pct — keeps
        # the ATR-scaled branch of _calculate_atr_targets from running away
        # on volatile bars, same ratio (1.5x / 1.6x) as live.
        self.max_target_pct = round(self.target_pct * 1.5, 4)
        self.max_sl_pct = round(self.stoploss_pct * 1.6, 4)
        # A backtest walks every 5-min bar in the date range and re-evaluates
        # the full strategy vote set on each one — for a multi-week range
        # across all 200 NIFTY 200 stocks this routinely takes well past a
        # minute. Running it synchronously inside the Flask request means
        # nginx's proxy_read_timeout (commonly 60s) fires first and the
        # client sees a 504, even though the backend was still working fine.
        # run_async() below runs it in a background thread instead; the
        # route returns immediately and the client polls /api/backtest/status
        # (same defensive pattern used elsewhere for background progress state).
        self._lock = threading.RLock()
        self._running = False
        self._progress = {
            'status': 'idle',   # idle | running | done | error
            'done': 0,
            'total': 0,
            'current': '',
            'results': None,
            'error': None,
        }

    @property
    def progress(self):
        with self._lock:
            return dict(self._progress)

    def _update(self, **kw):
        with self._lock:
            self._progress.update(kw)

    def run_async(self, kite, symbol_map, stock_universe, initial_wallet=100000, from_date=None, to_date=None):
        if self._running:
            return {'status': 'already_running'}
        self._running = True
        self._update(status='running', done=0, total=len(stock_universe),
                     current='Starting...', results=None, error=None)

        def _worker():
            try:
                results = self.run(
                    kite, symbol_map, stock_universe,
                    initial_wallet=initial_wallet, from_date=from_date, to_date=to_date,
                    progress_cb=self._update,
                )
                self._update(status='done', done=self._progress.get('total', 0),
                             current='Complete', results=results)
            except Exception as e:
                logger.error(f"Backtest worker error: {e}")
                traceback.print_exc()
                self._update(status='error', error=str(e))
            finally:
                self._running = False

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return {'status': 'started'}

    def run(self, kite, symbol_map, stock_universe, initial_wallet=100000, from_date=None, to_date=None, progress_cb=None):
        if from_date and to_date:
            start = datetime.fromisoformat(from_date)
            end = datetime.fromisoformat(to_date)
            if end.time() == datetime.min.time():
                end = end.replace(hour=23, minute=59, second=59)
            end = min(end, datetime.now())
        else:
            end = datetime.now()
            start = end - timedelta(days=30)

        logger.info("=" * 60)
        logger.info(f"BACKTEST START: {len(stock_universe)} stocks, {len(self.strategies_dict)} strategies")
        strategy_names = list(self.strategies_dict.keys())
        logger.info(f"  Strategies: {', '.join(strategy_names[:5])}{' ...' if len(strategy_names)>5 else ''}")
        logger.info(f"  Date range: {start} -> {end}  |  Mode: {self.trading_mode}")
        logger.info(f"  Target: {self.target_pct*100:.2f}%  |  Stop Loss: {self.stoploss_pct*100:.2f}%")
        logger.info(f"  Initial Wallet: Rs {initial_wallet:,.2f}")
        logger.info("=" * 60)

        margin_pct = 1.0 if self.trading_mode == 'DELIVERY' else 0.2
        trades = []
        wallet = initial_wallet
        positions = {}
        last_exit_time = {}
        max_open_pos = PaperTradingEngine.MAX_OPEN_POS

        def _release(pos_sym, position, exit_price, reason, close_time):
            t = self._close_trade(pos_sym, position, exit_price, reason, close_time)
            trades.append(t)
            nonlocal wallet
            margin_used = position['entry_price'] * position['qty'] * margin_pct
            wallet += margin_used + t['pnl']
            try:
                last_exit_time[pos_sym] = (
                    close_time if isinstance(close_time, datetime)
                    else pd.to_datetime(close_time).to_pydatetime()
                )
            except Exception:
                last_exit_time[pos_sym] = datetime.now()
            return t

        total_syms = len(stock_universe)
        # NOTE: no vote-percentage panel gate, regime filter, correlation
        # filter, gap filter, or entry-quality veto remain in this loop —
        # a strategy triggering is the only condition for a trade. See the
        # inline notes further down where each was removed.
        logger.info("Gate-free mode: a strategy trigger places a trade directly (matches live _check_signal)")

        min_bars_needed = _get_strategy_min_bars(self.strategies_dict)
        logger.info(f"Min-bar gate set to {min_bars_needed}")
        strat_timeframe = _get_strategy_timeframe(self.strategies_dict)
        logger.info(f"Timeframe set to {strat_timeframe} (from strategy module's own TIMEFRAME setting)")

        # One-time NIFTY 5-min history fetch spanning the whole backtest
        # range, used to reproduce live's market_trend/market_regime gates
        # (skip BREAKOUT-category strategies while the index is RANGING;
        # block a BUY/SELL on a HEAVYWEIGHTS symbol that fights the index
        # trend — see get_nifty_data()/_check_signal() in
        # PaperTradingEngine) without hitting the historical-data API on
        # every single bar-event. If the fetch fails, regime/correlation
        # gating is simply skipped for this run — same as live's own
        # fallback when get_nifty_data() returns None.
        nifty_ctx = self._fetch_nifty_context(kite, start, end)
        if nifty_ctx is not None and not nifty_ctx.empty:
            logger.info(f"NIFTY context loaded: {len(nifty_ctx)} bars (informational only — no longer used to gate trades)")
        else:
            logger.warning("NIFTY context unavailable — regime/correlation gates disabled for this run")

        symbol_data = {}
        for _sym_idx, sym in enumerate(stock_universe):
            if progress_cb:
                try:
                    progress_cb(done=_sym_idx, total=total_syms, current=sym)
                except Exception:
                    pass

            if sym not in symbol_map:
                logger.debug(f"SKIP {sym}: not in symbol_map")
                continue

            token = symbol_map[sym]['token']
            try:
                _hist_limiter.wait(f"backtest {sym}")
                data = kite.historical_data(token, start, end, strat_timeframe)
                if data:
                    _first_dt = data[0].get('date')
                    _last_dt = data[-1].get('date')
                    logger.info(
                        f"FETCHED {sym}: {len(data)} bars  "
                        f"[{_first_dt} -> {_last_dt}]  (requested {start} -> {end})"
                    )
                else:
                    logger.info(f"FETCHED {sym}: 0 bars returned  (requested {start} -> {end})")

                if not data or len(data) < min_bars_needed:
                    logger.debug(
                        f"SKIP {sym}: insufficient data "
                        f"({len(data) if data else 0} bars, need >= {min_bars_needed})"
                    )
                    continue

                df = pd.DataFrame(data)
                ind = Indicators.calculate_all(df)
                dt_series = pd.to_datetime(df['date']) if 'date' in df.columns else pd.to_datetime(df.index.to_series().reset_index(drop=True))
                symbol_data[sym] = {'df': df, 'ind': ind, 'dt': dt_series}
                logger.debug(f"Processing {sym}: {len(df)} bars, indicators loaded")
            except Exception as e:
                logger.error(f"Backtest fetch error on {sym}: {e}")
                import traceback
                logger.error(traceback.format_exc())

        events = []
        _loop_start = max(0, min_bars_needed - 1)
        for sym, sd in symbol_data.items():
            for i in range(_loop_start, len(sd['df'])):
                events.append((sd['dt'].iloc[i], sym, i))
        events.sort(key=lambda e: (e[0], e[1]))

        logger.info(
            f"Merged timeline: {len(events)} bar-events across {len(symbol_data)} symbols  "
            f"|  Global open-position cap = {max_open_pos} (matches live MAX_OPEN_POS)"
        )

        vote_log_count = {}
        for bar_time, sym, i in events:
            sd = symbol_data[sym]
            df, ind, dt_series = sd['df'], sd['ind'], sd['dt']
            df_slice = df.iloc[:i+1]
            ind_slice = ind.iloc[:i+1]
            if len(df_slice) < min_bars_needed:
                continue

            current_bar = df_slice.iloc[-1]
            ltp = float(current_bar['close'])
            bar_high = float(current_bar['high'])
            bar_low = float(current_bar['low'])
            try:
                bar_dt = bar_time if isinstance(bar_time, datetime) else pd.to_datetime(bar_time).to_pydatetime()
                bar_mins = bar_dt.hour * 60 + bar_dt.minute
            except Exception:
                bar_dt = datetime.now()
                bar_mins = 0

            if sym in positions:
                pos = positions[sym]
                exit_action = 'SELL' if pos['side'] == 'BUY' else 'BUY'
                if self.trading_mode == 'INTRADAY' and bar_mins >= PaperTradingEngine.SQUARE_OFF_TIME:
                    _eod_fill = self._slip(ltp, exit_action)
                    _eod_fill = min(_eod_fill, round(bar_high, 2)) if exit_action == 'BUY' else max(_eod_fill, round(bar_low, 2))
                    _release(sym, pos, _eod_fill, 'EOD_SQUAREOFF', bar_time)
                    del positions[sym]
                    logger.debug(f"  {sym} sq-off at {bar_dt.strftime('%H:%M')}")
                    continue
                
                if self.trading_mode == 'DELIVERY' and self.min_hold_days > 0:
                    days_held = (bar_dt.date() - pos['entry_date']).days
                    if days_held < self.min_hold_days:
                        continue

                exit_price = None
                exit_reason = None
                if pos['side'] == 'BUY':
                    if bar_low <= pos['stoploss']:
                        exit_price = self._slip(pos['stoploss'], exit_action)
                        exit_reason = 'STOP_LOSS'
                    elif bar_high >= pos['target']:
                        exit_price = self._slip(pos['target'], exit_action)
                        exit_reason = 'TARGET'
                else:
                    if bar_high >= pos['stoploss']:
                        exit_price = self._slip(pos['stoploss'], exit_action)
                        exit_reason = 'STOP_LOSS'
                    elif bar_low <= pos['target']:
                        exit_price = self._slip(pos['target'], exit_action)
                        exit_reason = 'TARGET'

                if exit_price is not None:
                    _release(sym, pos, exit_price, exit_reason, bar_time)
                    del positions[sym]
                    logger.debug(
                        f"  {sym} {pos['side']} {exit_reason} hit at "
                        f"{bar_dt.strftime('%Y-%m-%d %H:%M')} "
                        f"(bar H={bar_high:.2f} L={bar_low:.2f}) "
                        f"P&L={trades[-1]['pnl']:.2f}"
                    )
                continue

            if len(positions) >= max_open_pos:
                continue

            # Mirrors live's _check_signal(), which — regardless of trading
            # mode — refuses to evaluate a *new* entry in the first few
            # minutes of the session (9:15-9:18, so indicators/opening
            # range have bars to form) or once the market has closed:
            #   if MARKET_OPEN <= mins < MARKET_OPEN+NEW_ENTRY_WARMUP_MINUTES: return
            #   if mins >= MARKET_CLOSE: return
            # Previously this whole window check was nested inside
            # `self.trading_mode == 'INTRADAY'`, so a Delivery/CNC backtest
            # could open a position at 9:16 AM or 3:29 PM — something live
            # would always refuse no matter the mode.
            if bar_mins < PaperTradingEngine.MARKET_OPEN + PaperTradingEngine.NEW_ENTRY_WARMUP_MINUTES or bar_mins >= PaperTradingEngine.MARKET_CLOSE:
                continue
            # NO_NEW_TRADES_AFTER (14:30 cutoff) and the 9:15-14:00 trade
            # slot exist only to guarantee runway to square off an INTRADAY
            # (MIS) position by 15:15 — Delivery/CNC has no forced
            # square-off, so it may enter any time up to MARKET_CLOSE
            # (matches live's _open_position_nolock, which only applies
            # NO_NEW_TRADES_AFTER / _in_trade_slot when trading_mode ==
            # 'INTRADAY').
            if self.trading_mode == 'INTRADAY' and (
                bar_mins >= PaperTradingEngine.NO_NEW_TRADES_AFTER
                or not _in_trade_slot(bar_mins)
            ):
                continue

            # Mirrors live's cooldown (self.last_exit_time / COOLDOWN_MINUTES)
            # — backtest was previously able to re-enter a symbol on the very
            # next bar after closing it, which live never allows.
            if sym in last_exit_time:
                cooldown_elapsed = (bar_dt - last_exit_time[sym]).total_seconds() / 60
                if cooldown_elapsed < PaperTradingEngine.COOLDOWN_MINUTES:
                    continue

            df_w = df_slice.iloc[-min_bars_needed:].reset_index(drop=True)
            ind_w = ind_slice.iloc[-min_bars_needed:].reset_index(drop=True)
            b, s, _ = _strat_votes(df_w, ind_w, self.strategies_dict, self.strategy_performance)
            vtot = b + s
            buy_pct = (b / vtot * 100) if vtot > 0 else 50.0
            sell_pct = (s / vtot * 100) if vtot > 0 else 50.0

            vlc = vote_log_count.get(sym, 0)
            if vlc < 60:
                logger.debug(
                    f"  {sym} @ {bar_dt.strftime('%Y-%m-%d %H:%M')}  "
                    f"b={b:.1f} ({buy_pct:.0f}%)  s={s:.1f} ({sell_pct:.0f}%)"
                )
                vote_log_count[sym] = vlc + 1

            # NOTE: the MIN_VOTE_PCT panel gate, the regime filter
            # (skip_breakout), the correlation filter (fighting NIFTY
            # trend on a HEAVYWEIGHTS symbol), the gap-up/gap-down filter,
            # and the _check_entry_quality() veto (volume surge, candle
            # pattern, VWAP/RSI/EMA50 extension) that used to all sit in
            # this block have been removed entirely. A strategy triggering
            # (b/s >= 1.0) is now the ONLY condition — this mirrors the
            # same change in PaperTradingEngine._check_signal(), so
            # backtest results stay a faithful preview of live behavior.
            buy_ok = b >= 1.0
            sell_ok = s >= 1.0 and self.trading_mode != 'DELIVERY'
            if buy_ok and sell_ok:
                # Same tiebreak as live: whichever side scores higher wins
                # when both directions independently qualify.
                if sell_pct > buy_pct:
                    buy_ok = False
                else:
                    sell_ok = False

            side = None
            if buy_ok:
                side = 'BUY'
            elif sell_ok:
                side = 'SELL'
            if side is None:
                continue

            # Determine which individual strategies actually triggered on
            # this bar and pick a "best" one, mirroring live's
            # direction_triggered/best_strategy logic in _check_signal().
            # Purely for logging/labeling the trade now — no longer used
            # to gate anything.
            all_triggered = []
            for _name, _func in self.strategies_dict.items():
                try:
                    if _func(df_w, ind_w):
                        all_triggered.append(_name)
                except Exception:
                    continue
            direction_triggered = [
                n for n in all_triggered
                if AVAILABLE_STRATEGY_META.get(n, {}).get('direction', 'BOTH') in (side, 'BOTH')
            ]
            best_strategy = None
            best_sc = -1
            for _name in direction_triggered:
                _sc = 70 + len(direction_triggered) * 5
                if self._should_use_strategy(_name):
                    _sc += 10
                if _sc > best_sc:
                    best_sc = _sc
                    best_strategy = _name
            if not best_strategy:
                best_strategy = "VOTE_SIGNAL"

            price = ltp
            atr = float(ind_slice['atr'].iloc[-1]) if 'atr' in ind_slice.columns else None

            if atr and atr > 0:
                target, stoploss = self._calculate_atr_targets(price, atr, side)
            elif side == 'BUY':
                target = price * (1 + self.target_pct)
                stoploss = price * (1 - self.stoploss_pct)
            else:
                target = price * (1 - self.target_pct)
                stoploss = price * (1 + self.stoploss_pct)

            margin_per_share = price * margin_pct
            qty = int((wallet * 0.8) / margin_per_share) if margin_per_share > 0 else 0

            # ATR-based risk sizing no longer caps qty — mirrors live's
            # _smart_allocation_time_based(), which now always sizes to the
            # full 80%-of-wallet margin cap. ATR still sets target/stoploss
            # price levels elsewhere; it just doesn't cut quantity anymore.
            if atr and atr > 0 and qty > 0:
                risk_amount = wallet * PaperTradingEngine.RISK_PER_TRADE_PCT
                sl_distance = atr * PaperTradingEngine.ATR_SL_MULTIPLIER
                if sl_distance > 0:
                    risk_qty = int(risk_amount / sl_distance)
                    logger.debug(
                        f"  {sym} {side} sizing: margin_cap={qty} (80% wallet) "
                        f"| risk_qty would have been {risk_qty} (ATR-based, no longer applied)"
                    )

            
            if qty <= 0:
                logger.debug(f"  {sym} {side} signal but qty=0 (wallet={wallet:.2f}, margin_per_share={margin_per_share:.2f})")
                continue

            # Slippage-adjusted fill — mirrors live's
            # _get_l2_execution_price()/SLIPPAGE_PCT: a BUY pays slightly
            # above the reference price, a SELL receives slightly below it.
            # target/stoploss/sizing above stay on the raw reference price
            # (same basis live uses), only the recorded entry_price/wallet
            # debit use the slipped fill — matching live's
            # _open_position_nolock exactly.
            fill_price = self._slip(price, side)
            if side == 'BUY':
                fill_price = min(fill_price, round(bar_high, 2))
            else:
                fill_price = max(fill_price, round(bar_low, 2))

            positions[sym] = {
                'side': side,
                'entry_price': fill_price,
                'qty': qty,
                'target': target,
                'stoploss': stoploss,
                'entry_time': bar_time,
                'entry_date': bar_dt.date(),
                'strategy': best_strategy,
            }
            wallet -= fill_price * qty * margin_pct
            logger.info(
                f"BACKTEST {side} {sym} @ {bar_dt.strftime('%Y-%m-%d %H:%M')}  price={price:.2f} fill={fill_price:.2f}  qty={qty}  "
                f"strategy={best_strategy}  b={b:.1f}  s={s:.1f}  target={target:.2f}  sl={stoploss:.2f}  open={len(positions)}/{max_open_pos}"
            )

        for sym, pos in list(positions.items()):
            df = symbol_data[sym]['df']
            last_price = float(df['close'].iloc[-1])
            last_time = df['date'].iloc[-1] if 'date' in df.columns else datetime.now()
            exit_action = 'SELL' if pos['side'] == 'BUY' else 'BUY'
            _release(sym, pos, self._slip(last_price, exit_action), 'DATA_END', last_time)
            logger.debug(f"  {sym} closed at end of data: P&L={trades[-1]['pnl']:.2f}")
            del positions[sym]

        total_trades = len(trades)
        if total_trades == 0:
            logger.info("BACKTEST COMPLETE: No trades executed.")
            return {
                'total_trades': 0, 'win_rate': 0, 'net_pnl': 0, 'trades': [],
                'final_wallet': initial_wallet, 'mode': self.trading_mode,
                'target_pct': round(self.target_pct * 100, 2),
                'stoploss_pct': round(self.stoploss_pct * 100, 2),
                'min_hold_days': self.min_hold_days,
            }

        wins = sum(1 for t in trades if t['pnl'] > 0)
        net_pnl = sum(t['pnl'] for t in trades)
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        final_wallet = initial_wallet + net_pnl

        logger.info(f"BACKTEST COMPLETE: {total_trades} trades, win rate {win_rate:.1f}%, net P&L Rs {net_pnl:.2f}")
        logger.info("=" * 60)

        return {
            'total_trades': total_trades,
            'win_trades': wins,
            'loss_trades': total_trades - wins,
            'win_rate': round(win_rate, 2),
            'net_pnl': round(net_pnl, 2),
            'final_wallet': round(final_wallet, 2),
            'trades': trades[-50:],
            'mode': self.trading_mode,
            'target_pct': round(self.target_pct * 100, 2),
            'stoploss_pct': round(self.stoploss_pct * 100, 2),
            'min_hold_days': self.min_hold_days,
        }

    def _close_trade(self, sym, pos, exit_price, reason, exit_time=None):
        entry = pos['entry_price']
        qty = pos['qty']
        side = pos['side']
        # Exact same charge model as PaperTradingEngine._close_position_nolock —
        # real brokerage/STT/exchange/GST/stamp-duty based on turnover. The old
        # `abs(pnl) * 0.001` here scaled charges off *profit*, not turnover, so
        # it was off by ~2-3 orders of magnitude vs real Zerodha charges and made
        # backtest P&L meaningless as a preview of live results.
        if side == 'BUY':
            chg = calc_zerodha_charges(entry, exit_price, qty)
        else:
            chg = calc_zerodha_charges(exit_price, entry, qty)
        gross_pnl = chg['gross_pnl']
        net_pnl = chg['net_pnl']
        entry_time = pos.get('entry_time')

        def _fmt(t):
            if t is None:
                return None
            if isinstance(t, str):
                return t
            try:
                return t.isoformat()
            except Exception:
                return str(t)

        entry_iso = _fmt(entry_time)
        return {
            'symbol': sym,
            'side': side,
            'entry_price': entry,
            'exit_price': exit_price,
            'qty': qty,
            'gross_pnl': round(gross_pnl, 2),
            'pnl': round(net_pnl, 2),
            'total_charges': round(chg['total_charges'], 2),
            'brokerage': round(chg['brokerage'], 2),
            'stt': round(chg['stt'], 2),
            'exchange_charge': round(chg['exchange_charge'], 2),
            'gst': round(chg['gst'], 2),
            'stamp_duty': round(chg['stamp_duty'], 2),
            'exit_reason': reason,
            'strategy': pos.get('strategy', 'VOTE_SIGNAL'),
            'entry_time': entry_iso,
            'exit_time': _fmt(exit_time),
            'date': entry_iso[:10] if entry_iso else '',
            'target': round(pos.get('target', 0), 2) if pos.get('target') is not None else None,
            'stoploss': round(pos.get('stoploss', 0), 2) if pos.get('stoploss') is not None else None,
        }

    def _fetch_nifty_context(self, kite, start, end):
        """One-time fetch of NIFTY 50 5-min history spanning the backtest
        date range, used to reproduce live's market_trend/market_regime
        checks (see get_nifty_data() + _check_signal() in
        PaperTradingEngine) without hitting the historical-data API on
        every single bar-event. Returns a DataFrame sorted by time with
        columns ['dt','change_pct','adx'], or None if the fetch fails —
        in which case regime/correlation gating is skipped for this run,
        matching live's own fallback when get_nifty_data() returns None.
        """
        try:
            # Pad the start a bit so the first bars of the range still have
            # enough history behind them for ADX(14) to be non-NaN.
            fetch_start = start - timedelta(days=5)
            _hist_limiter.wait("backtest nifty context")
            data = kite.historical_data(256265, fetch_start, end, "3minute")
            if not data or len(data) < 20:
                return None
            df = pd.DataFrame(data)
            ind = Indicators.calculate_all(df)
            dt = pd.to_datetime(df['date']) if 'date' in df.columns else pd.to_datetime(df.index)
            change_pct = df['close'].pct_change() * 100
            adx = ind['adx'] if 'adx' in ind.columns else pd.Series(0.0, index=df.index)
            ctx = pd.DataFrame({
                'dt': dt.reset_index(drop=True),
                'change_pct': change_pct.reset_index(drop=True),
                'adx': adx.reset_index(drop=True),
            })
            ctx = ctx.sort_values('dt').reset_index(drop=True)
            return ctx
        except Exception as e:
            logger.warning(f"Backtest NIFTY context fetch failed: {e}")
            return None

    def _market_context_at(self, ctx, bar_dt):
        """Look up the NIFTY market_trend/market_regime as of the most
        recent NIFTY bar at or before bar_dt. Mirrors the thresholds in
        PaperTradingEngine._check_signal() (change > 0.3% => BULLISH,
        < -0.3% => BEARISH; ADX > 25 => TRENDING else RANGING)."""
        if ctx is None or ctx.empty:
            return None, None
        try:
            pos = ctx['dt'].searchsorted(bar_dt, side='right') - 1
            if pos < 0:
                return None, None
            row = ctx.iloc[int(pos)]
            change_pct = row['change_pct']
            adx = row['adx']
            if pd.isna(change_pct):
                change_pct = 0.0
            if pd.isna(adx):
                adx = 0.0
            market_trend = "BULLISH" if change_pct > 0.3 else "BEARISH" if change_pct < -0.3 else "NEUTRAL"
            market_regime = "TRENDING" if adx > 25 else "RANGING"
            return market_trend, market_regime
        except Exception:
            return None, None

    def _should_use_strategy(self, name):
        # Mirrors PaperTradingEngine._should_use_strategy, using this
        # backtest run's strategy_performance snapshot instead of the live
        # engine's continuously-updating one.
        sp = (self.strategy_performance or {}).get(name)
        if not sp:
            return True
        total = sp.get('total_trades', 0)
        if total < PaperTradingEngine.STRATEGY_MIN_TRADES:
            return True
        return sp.get('win_rate', 0) >= PaperTradingEngine.STRATEGY_MIN_WIN_RATE

    @staticmethod
    def _slip(price, action):
        # Mirrors PaperTradingEngine._get_execution_price's slippage
        # direction: a BUY pays slightly above the reference price, a SELL
        # receives slightly below it. Applied at both entry and exit so
        # backtest fills aren't systematically better than live's ever
        # would be.
        if action == 'BUY':
            return round(price * (1.0 + PaperTradingEngine.SLIPPAGE_PCT), 2)
        else:
            return round(price * (1.0 - PaperTradingEngine.SLIPPAGE_PCT), 2)

    def _calculate_atr_targets(self, price, atr, side):
        # Exact port of PaperTradingEngine._calculate_atr_targets — same
        # 3-tier atr_pct branching (high-vol / low-vol / normal) and the
        # same hard min/max caps via self.max_target_pct/self.max_sl_pct.
        # The previous version here used an unrelated linear-scaling
        # formula that didn't reproduce live's target/SL levels at all.
        atr_pct = (atr / price) * 100 if price > 0 else 1.0
        if atr_pct > 2.0:
            if side == 'BUY':
                sl = round(price - atr * 1.0, 2)
                tgt = round(price + atr * 1.5, 2)
            else:
                sl = round(price + atr * 1.0, 2)
                tgt = round(price - atr * 1.5, 2)
        elif atr_pct < 0.5:
            tight_tgt_pct = self.target_pct * 0.5
            tight_sl_pct = self.stoploss_pct * 0.6
            if side == 'BUY':
                tgt = round(price * (1.0 + tight_tgt_pct), 2)
                sl = round(price * (1.0 - tight_sl_pct), 2)
            else:
                tgt = round(price * (1.0 - tight_tgt_pct), 2)
                sl = round(price * (1.0 + tight_sl_pct), 2)
        else:
            if side == 'BUY':
                tgt = round(price * (1.0 + self.target_pct), 2)
                sl = round(price * (1.0 - self.stoploss_pct), 2)
            else:
                tgt = round(price * (1.0 - self.target_pct), 2)
                sl = round(price * (1.0 + self.stoploss_pct), 2)
        if side == 'BUY':
            tgt = min(tgt, round(price * (1.0 + self.max_target_pct), 2))
            sl = max(sl, round(price * (1.0 - self.max_sl_pct), 2))
        else:
            tgt = max(tgt, round(price * (1.0 - self.max_target_pct), 2))
            sl = min(sl, round(price * (1.0 + self.max_sl_pct), 2))
        if side == 'BUY':
            if sl >= price or tgt <= price or tgt <= sl:
                tgt = round(price * (1.0 + self.target_pct), 2)
                sl = round(price * (1.0 - self.stoploss_pct), 2)
        else:
            if tgt >= price or sl <= price or tgt >= sl:
                tgt = round(price * (1.0 - self.target_pct), 2)
                sl = round(price * (1.0 + self.stoploss_pct), 2)
        return tgt, sl

    def _check_entry_quality(self, df, ind, side, symbol, bar_dt, strategy_name=None):
        """Exact port of PaperTradingEngine._check_entry_quality, adapted to
        take the bar's timestamp (bar_dt) instead of datetime.now(). Live
        vetoes an entry here (extension from VWAP, RSI overbought/oversold,
        distance from EMA50, adverse candle pattern on the trigger bar)
        AFTER the vote passes — backtest previously had no equivalent gate
        at all, so it was taking every vote-qualified signal live would
        actually reject. See the live method for full per-check rationale;
        logic kept field-for-field identical here."""
        try:
            price = float(df['close'].iloc[-1])
            now_mins = bar_dt.hour * 60 + bar_dt.minute

            meta = AVAILABLE_STRATEGY_META.get(strategy_name, {})
            category = meta.get('category', 'default')
            skip_extension_checks = category in ('breakout', 'momentum')
            # See matching comment in PaperTradingEngine._check_entry_quality
            # — strategies that mark skip_quality_checks=True in their own
            # strategy_meta bypass the volume-surge and candle-pattern
            # vetoes entirely, not just the extension checks below. Without
            # this, backtest silently rejected every single vote-qualified
            # EMA_MOMENTUM_BUY/SELL signal (visible in the log as "BUY: ..."
            # lines from the strategy firing, immediately followed by
            # "BACKTEST COMPLETE: No trades executed" because none of them
            # ever passed this gate).
            if meta.get('skip_quality_checks'):
                return True, None

            def _iv(key, fallback=0.0):
                try:
                    v = float(ind[key].iloc[-1])
                    return fallback if (isinstance(v, float) and np.isnan(v)) else v
                except Exception:
                    return fallback
            ema50 = _iv('ema_50', price)
            rsi = _iv('rsi', 50.0)
            vwap = _iv('vwap', 0.0)
            vwap_u1 = _iv('vwap_upper1', price * 1.02)
            vwap_l1 = _iv('vwap_lower1', price * 0.98)
            roc5 = _iv('roc5', 0.0)
            htf_bull = _iv('htf_bull', 0.5)
            atr = _iv('atr', 0.0)
            avg_vol = float(df['volume'].iloc[-10:].mean()) if len(df) >= 10 else float(df['volume'].mean())
            cur_vol = float(df['volume'].iloc[-1])
            vol_mult = 1.0 if now_mins < 10*60 else 0.5 if now_mins < 13*60 else 0.4
            if cur_vol < avg_vol * vol_mult:
                return False, f"Low volume {int(cur_vol):,} < {int(avg_vol*vol_mult):,} ({vol_mult}x avg)"
            if cur_vol < avg_vol * PaperTradingEngine.MIN_VOL_SURGE:
                return False, f"Volume surge insufficient: {cur_vol/avg_vol:.1f}x < {PaperTradingEngine.MIN_VOL_SURGE}x"

            cp = {}
            try:
                n = len(df) - 1
                c = float(df['close'].iloc[n]); o = float(df['open'].iloc[n])
                h = float(df['high'].iloc[n]); l = float(df['low'].iloc[n])
                rng = max(h - l, 1e-9); body = abs(c - o); body_r = body / rng
                uw_r = (h - max(c, o)) / rng; lw_r = (min(c, o) - l) / rng
                bull = c >= o; bear = c < o
                cp['DOJI'] = body_r < 0.10
                cp['SPINNING_TOP'] = (0.10 <= body_r <= 0.30 and uw_r > 0.25 and lw_r > 0.25)
                cp['HAMMER'] = (lw_r > 0.60 and body_r < 0.30 and uw_r < 0.15 and bull)
                cp['INVERTED_HAMMER'] = (uw_r > 0.60 and body_r < 0.30 and lw_r < 0.15 and bull)
                cp['SHOOTING_STAR'] = (uw_r > 0.60 and body_r < 0.30 and lw_r < 0.15 and bear)
                cp['HANGING_MAN'] = (lw_r > 0.60 and body_r < 0.30 and uw_r < 0.15 and bear)
                cp['BULL_MARUBOZU'] = (body_r > 0.85 and bull and uw_r < 0.08 and lw_r < 0.08)
                cp['BEAR_MARUBOZU'] = (body_r > 0.85 and bear and uw_r < 0.08 and lw_r < 0.08)
                if n >= 1:
                    pc = float(df['close'].iloc[n-1]); po = float(df['open'].iloc[n-1])
                    ph = float(df['high'].iloc[n-1]); pl = float(df['low'].iloc[n-1])
                    pbull = pc > po; pbear = pc < po
                    pb = abs(pc - po); pm = (po + pc) / 2.0
                    cp['BULL_ENGULFING'] = (pbear and bull and o <= pc and c >= po and body >= pb)
                    cp['BEAR_ENGULFING'] = (pbull and bear and o >= pc and c <= po and body >= pb)
                    cp['PIERCING_LINE'] = (pbear and bull and o < pl and c > pm and c < po)
                    cp['DARK_CLOUD_COVER'] = (pbull and bear and o > ph and c < pm and c > pc)
                    cp['TWEEZER_TOP'] = (pbull and bear and abs(h - ph) / rng < 0.05)
                    cp['TWEEZER_BOTTOM'] = (pbear and bull and abs(l - pl) / rng < 0.05)
                if n >= 2:
                    c2 = float(df['close'].iloc[n-2]); o2 = float(df['open'].iloc[n-2])
                    c1 = float(df['close'].iloc[n-1]); o1 = float(df['open'].iloc[n-1])
                    h1 = float(df['high'].iloc[n-1]); l1 = float(df['low'].iloc[n-1])
                    rng1 = max(h1 - l1, 1e-9); body1 = abs(c1 - o1) / rng1
                    mid2 = (o2 + c2) / 2.0
                    cp['MORNING_STAR'] = (c2 < o2 and body1 < 0.30 and bull and c > mid2)
                    cp['EVENING_STAR'] = (c2 > o2 and body1 < 0.30 and bear and c < mid2)
                    cp['THREE_WHITE_SOLDIERS'] = (c2 > o2 and c1 > o1 and bull and c1 > c2 and c > c1 and body_r > 0.50)
                    cp['THREE_BLACK_CROWS'] = (c2 < o2 and c1 < o1 and bear and c1 < c2 and c < c1 and body_r > 0.50)
            except Exception:
                pass

            if side == 'BUY':
                bearish_veto = ['SHOOTING_STAR','EVENING_STAR','BEAR_ENGULFING','DARK_CLOUD_COVER','HANGING_MAN','THREE_BLACK_CROWS','BEAR_MARUBOZU','TWEEZER_TOP']
                triggered = [p for p in bearish_veto if cp.get(p)]
                if triggered:
                    return False, f"Bearish candle pattern on trigger bar: {', '.join(triggered)}"
            if side == 'SELL':
                bullish_veto = ['HAMMER','MORNING_STAR','BULL_ENGULFING','PIERCING_LINE','INVERTED_HAMMER','THREE_WHITE_SOLDIERS','BULL_MARUBOZU','TWEEZER_BOTTOM']
                triggered = [p for p in bullish_veto if cp.get(p)]
                if triggered:
                    return False, f"Bullish candle pattern on trigger bar: {', '.join(triggered)}"

            if not skip_extension_checks:
                if side == 'BUY':
                    if vwap_u1 > 0 and price > vwap_u1 and roc5 > 1.5:
                        return False, f"Price extended above VWAP+1sigma ({price:.2f} > {vwap_u1:.2f}) ROC5={roc5:.1f}%"
                    if atr > 0 and vwap > 0 and (price - vwap) > 2.0 * atr:
                        return False, f"Price > 2xATR above VWAP ({price:.2f} vs {vwap:.2f})"
                if side == 'SELL':
                    if vwap_l1 > 0 and price < vwap_l1 and roc5 < -1.5:
                        return False, f"Price extended below VWAP-1sigma ({price:.2f} < {vwap_l1:.2f}) ROC5={roc5:.1f}%"
                    if atr > 0 and vwap > 0 and (vwap - price) > 2.0 * atr:
                        return False, f"Price > 2xATR below VWAP ({price:.2f} vs {vwap:.2f})"
                if side == 'BUY' and rsi > 72 and htf_bull > 0.85:
                    return False, f"Overbought — RSI {rsi:.1f} > 72, HTF bull {htf_bull:.2f} > 0.85"
                if side == 'SELL' and rsi < 28 and htf_bull < 0.15:
                    return False, f"Oversold — RSI {rsi:.1f} < 28, HTF bull {htf_bull:.2f} < 0.15"
                if side == 'BUY' and ema50 > 0 and price < ema50 * 0.98:
                    return False, f"BUY price {price:.2f} >2% below EMA50 {ema50:.2f} — counter-trend"
                if side == 'SELL' and ema50 > 0 and price > ema50 * 1.02:
                    return False, f"SELL price {price:.2f} >2% above EMA50 {ema50:.2f} — counter-trend"
                if side == 'BUY' and rsi > 75:
                    return False, f"BUY into overbought RSI {rsi:.1f} > 75"
                if side == 'SELL' and rsi < 25:
                    return False, f"SELL into oversold RSI {rsi:.1f} < 25"
                if side == 'SELL' and vwap > 0 and price < vwap * 0.99:
                    return False, f"SELL already below VWAP ({price:.2f} < {vwap:.2f}) — chasing down"

            return True, None
        except Exception:
            return True, None

# ==================== FLASK APP ====================
app = Flask(__name__)

_flask_secret = os.getenv("FLASK_SECRET_KEY")
if not _flask_secret:
    raise ValueError(
        "FLASK_SECRET_KEY not set in .env file — required for session persistence "
        "across worker processes/restarts on production servers."
    )
app.secret_key = _flask_secret

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Only enable this if you are serving over HTTPS (recommended on VPS):
    SESSION_COOKIE_SECURE=os.getenv("FORCE_HTTPS", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
)

# ==================== LOGIN RATE LIMITING ====================
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 900  # 15 minutes
_login_attempts = {}
_login_attempts_lock = Lock()

def _login_rate_limited(key):
    with _login_attempts_lock:
        now = time.time()
        attempts = [t for t in _login_attempts.get(key, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _login_attempts[key] = attempts
        return len(attempts) >= _LOGIN_MAX_ATTEMPTS

def _login_record_failure(key):
    with _login_attempts_lock:
        _login_attempts.setdefault(key, []).append(time.time())

def _login_clear(key):
    with _login_attempts_lock:
        _login_attempts.pop(key, None)

# ==================== FLASK ROUTES ====================

@app.route('/login', methods=['GET', 'POST'])
def login():
    users = UserManager.load_users()
    error = None
    if request.method == 'POST':
        user_id = (request.form.get('user_id') or '').strip()
        password = request.form.get('password') or ''
        client_ip = request.remote_addr or 'unknown'
        rl_key = f"{client_ip}:{user_id}"

        if _login_rate_limited(rl_key):
            error = "Too many failed attempts. Please try again in 15 minutes."
        else:
            user_data = users.get(user_id)
            pw_hash = (user_data or {}).get('password_hash') or ''
            # check_password_hash is called even on a missing user/hash (against a dummy
            # hash) so response timing doesn't reveal whether the user_id exists.
            valid = check_password_hash(pw_hash, password) if pw_hash else False
            if user_data and pw_hash and valid:
                _login_clear(rl_key)
                session.clear()
                session.permanent = True
                session['user_id'] = user_id
                logger.info(f"Login OK: {user_id} from {client_ip}")
                if UserManager.ensure_authenticated(user_id):
                    return redirect('/')
                else:
                    return redirect(url_for('auth', user_id=user_id))
            else:
                _login_record_failure(rl_key)
                logger.warning(f"Failed login attempt for user_id='{user_id}' from {client_ip}")
                error = "Invalid username or password."
    return render_template_string(LOGIN_HTML, users=users, error=error)


@app.route('/auth/<user_id>')
def auth(user_id):
    if session.get('user_id') != user_id:
        return redirect('/login')
    user_data = UserManager.get_user_data(user_id)
    if not user_data:
        return "User not found", 404
    
    redirect_url = os.getenv("REDIRECT_URL", "https://rahulintratrading.online/api/broker/callback")
    kite = KiteConnect(api_key=user_data["kite_api_key"])
    
    session['redirect_url'] = redirect_url
    config = Config(user_id)
    if os.path.exists(config.TOKEN_FILE):
        try:
            with open(config.TOKEN_FILE, "r") as f:
                access_token = f.read().strip()
            kite.set_access_token(access_token)
            _other_limiter.wait("profile")
            kite.profile()
            UserManager._kites[user_id] = kite
            return redirect('/')
        except Exception:
            pass
    
    UserManager._kites[user_id] = kite
    login_url = kite.login_url()
    if 'redirect_url' not in login_url:
        import urllib.parse
        parsed = list(urllib.parse.urlparse(login_url))
        query = dict(urllib.parse.parse_qsl(parsed[4]))
        query['redirect_url'] = redirect_url
        parsed[4] = urllib.parse.urlencode(query)
        login_url = urllib.parse.urlunparse(parsed)
    
    session['pending_user'] = user_id
    return redirect(login_url)

@app.route('/api/broker/callback')
def oauth_callback():
    request_token = request.args.get('request_token')
    if not request_token:
        return "Missing request_token", 400
    
    user_id = session.get('pending_user')
    if not user_id:
        return "No user in session", 400
    
    user_data = UserManager.get_user_data(user_id)
    if not user_data:
        return "User not found", 404
    
    try:
        kite = UserManager.get_kite(user_id)
        _other_limiter.wait("generate_session")
        data = kite.generate_session(request_token, api_secret=user_data["kite_api_secret"])
        access_token = data["access_token"]
        UserManager.set_access_token(user_id, access_token)
        session.pop('pending_user', None)
        get_instrument_cache(kite)
        return redirect('/')
    except Exception as e:
        return f"Error exchanging token: {e}", 500

@app.route('/api/user/update-keys', methods=['POST'])
def update_user_keys():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'msg': 'Not logged in'}), 401

    data = request.json
    api_key = data.get('kite_api_key')
    api_secret = data.get('kite_api_secret')

    if not api_key or not api_secret:
        return jsonify({'status': 'error', 'msg': 'Both key and secret are required'}), 400

    try:
        with open("users.json", "r") as f:
            users = json.load(f)
    except FileNotFoundError:
        return jsonify({'status': 'error', 'msg': 'users.json not found'}), 500

    if user_id not in users:
        return jsonify({'status': 'error', 'msg': 'User not found'}), 404

    enc_key = encrypt_secret(api_key)
    enc_secret = encrypt_secret(api_secret)

    users[user_id]['kite_api_key'] = enc_key
    users[user_id]['kite_api_secret'] = enc_secret

    with open("users.json", "w") as f:
        json.dump(users, f, indent=2)

    UserManager.reload_users()

    return jsonify({'status': 'ok', 'msg': 'API keys updated successfully'})

@app.route('/api/user/update-mode', methods=['POST'])
def update_user_mode():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'msg': 'Not logged in'}), 401

    data = request.json
    mode = data.get('mode')
    if mode not in ('INTRADAY', 'DELIVERY'):
        return jsonify({'status': 'error', 'msg': 'Invalid mode'}), 400

    try:
        UserManager.set_user_mode(user_id, mode)
        if user_id in UserManager._paper_engines:
            UserManager._paper_engines[user_id].stop()
            del UserManager._paper_engines[user_id]
        return jsonify({'status': 'ok', 'msg': f'Trading mode set to {mode}'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/api/user/update-strategy', methods=['POST'])
def update_user_strategy():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'msg': 'Not logged in'}), 401

    data = request.json
    strategy_name = data.get('strategy')
    if not strategy_name or strategy_name not in AVAILABLE_STRATEGIES:
        return jsonify({'status': 'error', 'msg': 'Invalid strategy'}), 400

    try:
        UserManager.set_user_strategy(user_id, strategy_name)
        if user_id in UserManager._paper_engines:
            UserManager._paper_engines[user_id].stop()
            del UserManager._paper_engines[user_id]
        return jsonify({'status': 'ok', 'msg': f'Strategy set to {strategy_name}'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/api/user/update-risk', methods=['POST'])
def update_user_risk():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'msg': 'Not logged in'}), 401

    data = request.json or {}
    try:
        rc = UserManager.set_user_risk_config(user_id, data)
    except ValueError as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 400
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)}), 500

    # Target/SL feed straight into position sizing math on the live engine —
    # restart it (same as a mode/strategy change) so the new values take
    # effect immediately instead of only on next process restart.
    if user_id in UserManager._paper_engines:
        UserManager._paper_engines[user_id].stop()
        del UserManager._paper_engines[user_id]
    return jsonify({'status': 'ok', 'msg': 'Target/Stop-Loss settings updated', 'risk_config': rc})

@app.route('/api/backtest/run', methods=['POST'])
def run_backtest():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'msg': 'Not logged in'}), 401

    data = request.json
    wallet = data.get('wallet', 100000)
    from_date = data.get('from_date')
    to_date = data.get('to_date')
    mode = data.get('mode') or UserManager.get_user_mode(user_id)
    if mode not in ('INTRADAY', 'DELIVERY'):
        mode = 'INTRADAY'

    # Target/SL: use whatever the request sent (per-run override from the
    # Backtest tab), falling back to the user's saved Settings for this mode.
    risk_config = UserManager.get_user_risk_config(user_id)
    if mode == 'DELIVERY':
        default_tgt_ui = risk_config['target_pct_delivery']
        default_sl_ui = risk_config['stoploss_pct_delivery']
    else:
        default_tgt_ui = risk_config['target_pct_intraday']
        default_sl_ui = risk_config['stoploss_pct_intraday']
    try:
        target_pct_ui = float(data.get('target_pct')) if data.get('target_pct') not in (None, '') else default_tgt_ui
    except (TypeError, ValueError):
        target_pct_ui = default_tgt_ui
    try:
        stoploss_pct_ui = float(data.get('stoploss_pct')) if data.get('stoploss_pct') not in (None, '') else default_sl_ui
    except (TypeError, ValueError):
        stoploss_pct_ui = default_sl_ui
    if not (0 < target_pct_ui <= 20):
        target_pct_ui = default_tgt_ui
    if not (0 < stoploss_pct_ui <= 20):
        stoploss_pct_ui = default_sl_ui

    # Minimum hold (calendar days) before a DELIVERY/CNC position can be
    # closed by TARGET/STOP_LOSS — user-controlled from the Backtest tab
    # (per-run override), falling back to the saved Settings value.
    # Meaningless for INTRADAY (forced to 0 inside BacktestEngine).
    default_mhd = risk_config.get('min_hold_days_delivery', 1)
    try:
        min_hold_days = int(data.get('min_hold_days')) if data.get('min_hold_days') not in (None, '') else default_mhd
    except (TypeError, ValueError):
        min_hold_days = default_mhd
    min_hold_days = max(0, min(30, min_hold_days))

    kite = UserManager.get_kite(user_id)
    pe = UserManager.get_paper_engine(user_id)
    strategy_name, strategies_dict = UserManager.get_user_strategy(user_id)

    symbol_map = get_symbol_map()
    stock_universe = [s for s in NIFTY200_SYMBOLS if s in symbol_map]
    if not stock_universe:
        return jsonify({'status': 'error', 'msg': 'NIFTY 200 symbol universe unavailable — check symbol map / broker connection'}), 400

    existing = UserManager._backtest_engines.get(user_id)
    if existing and existing._running:
        return jsonify({'status': 'already_running'})

    # Fire-and-poll instead of blocking this request — see BacktestEngine
    # docstring for why: a wide date range can take well over nginx's
    # default proxy_read_timeout and surface as a 504 even on success.
    backtest = BacktestEngine(strategies_dict, trading_mode=mode,
                               target_pct=target_pct_ui / 100.0, stoploss_pct=stoploss_pct_ui / 100.0,
                               min_hold_days=min_hold_days,
                               strategy_performance=dict(pe.strategy_performance or {}))
    UserManager._backtest_engines[user_id] = backtest
    result = backtest.run_async(kite, symbol_map, stock_universe, initial_wallet=wallet, from_date=from_date, to_date=to_date)
    return jsonify(result)

@app.route('/api/backtest/status')
def backtest_status():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'status': 'error', 'msg': 'Not logged in'}), 401
    engine = UserManager._backtest_engines.get(user_id)
    if not engine:
        return jsonify({'status': 'idle'})
    return jsonify(engine.progress)

# ─── HELPER FOR SYMBOL MAP ──────────────────────────────────
def get_symbol_map():
    global _instrument_cache, SYMBOL_MAP
    if _instrument_cache is None:
        user_id = session.get('user_id')
        if user_id:
            kite = UserManager.get_kite(user_id)
            get_instrument_cache(kite)
        else:
            return {}
    sm = {s['symbol']: s for s in _instrument_cache}
    SYMBOL_MAP = sm
    return sm

# ─── HELPER FOR USER ENGINES ──────────────────────────────
def get_user_engines():
    user_id = session.get('user_id')
    if not user_id:
        return None, None, None, None
    if not UserManager.ensure_authenticated(user_id):
        return None, None, None, None
    kite = UserManager.get_kite(user_id)
    pe = UserManager.get_paper_engine(user_id)
    sector = UserManager.get_sector_monitor(user_id)
    return user_id, kite, pe, sector

# ─── MAIN INDEX ─────────────────────────────────────────────
@app.route('/')
def index():
    user_id = session.get('user_id')
    if not user_id:
        return redirect('/login')
    if not UserManager.ensure_authenticated(user_id):
        return redirect(url_for('auth', user_id=user_id))
    pe = UserManager.get_paper_engine(user_id)
    symbol_map = get_symbol_map()
    content = gen_paper_tab(pe)
    # Pass only the list of strategy names (JSON serializable)
    strategy_names = list(AVAILABLE_STRATEGIES.keys())
    current_strategy = UserManager.get_user_strategy(user_id)[0]
    current_mode = UserManager.get_user_mode(user_id)
    current_risk = UserManager.get_user_risk_config(user_id)
    return render_template_string(HTML,
        content=content,
        all_symbols=sorted(symbol_map.keys()),
        universe_count=len(NIFTY200_SYMBOLS),
        available_strategies=strategy_names,
        current_strategy=current_strategy,
        current_mode=current_mode,
        current_risk=current_risk
    )

@app.template_filter('fmt')
def fmt_f(v):
    try:
        return format(v, ',')
    except:
        return v

@app.route('/paper/wallet', methods=['POST'])
def paper_wallet():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        d = request.json
        amount = d.get('amount', 0)
        amount = float(amount)
        if amount <= 0:
            return jsonify({'status': 'error', 'msg': 'Must be positive'})
        with pe._lock:
            used = sum(p.get('margin_used', p['entry_price'] * p['qty']) 
                      for p in pe.data['positions'].values())
            if amount < used:
                return jsonify({'status': 'error', 'msg': f'Cannot set below used margin {used:.2f}'})
        pe.set_wallet(amount)
        return jsonify({'status': 'ok', 'wallet': amount})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/summary')
def paper_summary():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        smry = pe.summary()
        return jsonify(smry)
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/margin-info')
def paper_margin_info():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        symbols = list(pe.data.get("positions", {}).keys())
        result = {}
        for sym in symbols:
            try:
                prices = pe._get_batch_ltp([sym])
                ltp = prices.get(sym)
                if not ltp:
                    pos = pe.data["positions"].get(sym)
                    ltp = pos["entry_price"] if pos else 100.0
                margin_per, margin_pct, source = pe._get_actual_margin(sym, ltp)
                result[sym] = {
                    "ltp": round(ltp, 2),
                    "margin_per_share": round(margin_per, 2),
                    "margin_pct": round(margin_pct * 100, 1),
                    "leverage": round(1 / margin_pct, 2) if margin_pct > 0 else 5.0,
                    "source": source,
                }
            except Exception as e:
                result[sym] = {"error": str(e), "margin_pct": 20.0, "leverage": 5.0, "source": "error"}
        return jsonify({"margins": result, "count": len(result)})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/orders')
def paper_orders():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        return jsonify({'orders': pe.data.get('orders', [])})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/trades')
def paper_trades():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        return jsonify({'trades': pe.data.get('trades', [])})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/exit', methods=['POST'])
def paper_exit():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        d = request.json
        sym = d.get('symbol', '').upper().strip()
        if not sym:
            return jsonify({'status': 'error', 'msg': 'No symbol'})
        ltp = pe._get_live_ltp(sym)
        if ltp is None:
            with pe._lock:
                if sym in pe.data['positions']:
                    ltp = pe.data['positions'][sym]['entry_price']
        result = pe.force_exit(sym, 'MANUAL_EXIT')
        if result:
            return jsonify({
                'status': 'ok',
                'msg': f'Closed {sym}',
                'trade': {
                    'symbol': result['symbol'],
                    'pnl': result['pnl'],
                    'exit_price': result['exit_price']
                }
            })
        else:
            return jsonify({'status': 'error', 'msg': f'No position for {sym}'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/signal-logs')
def paper_signal_logs():
    try:
        _, _, pe, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        date = request.args.get('date', '')
        status = request.args.get('status', '')
        logs = pe.get_signal_logs(
            date=date if date else None,
            status=status if status else None,
            limit=300
        )
        return jsonify({'logs': logs})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/sector/status')
def sector_status():
    try:
        _, _, _, sector = get_user_engines()
        if not sector:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        return jsonify({
            'running': sector._running,
            'last_signals': sector._last_signal_time,
            'cooldown_seconds': sector._cooldown_seconds
        })
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/trade-slots')
def trade_slots_status():
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    return jsonify({
        'current_time': now.strftime('%H:%M:%S'),
        'current_mins': mins,
        'in_slot': _in_trade_slot(mins),
        'slot_label': _slot_label(mins),
        'slots': [
            {'name': 'SLOT-1', 'label': '9:15–14:30', 'quality': '⭐⭐⭐⭐⭐', 'type': 'Trading Window',
            'active': 9*60+15 <= mins <= 14*60+30},
        ],
    })

@app.route('/market-indices')
def market_indices():
    try:
        _, kite, _, _ = get_user_engines()
        if not kite:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        indices = [
            ('NSE:NIFTY 50', 'NIFTY', 'index'),
            ('NSE:NIFTY BANK', 'BANKNIFTY', 'index'),
            ('NSE:INDIA VIX', 'VIX', 'vix'),
            ('BSE:SENSEX', 'SENSEX', 'index')
        ]
        out = []
        for ts, label, kind in indices:
            try:
                _quote_limiter.wait(f"index {label}")
                q = kite.ltp([ts])
                ltp_val = float(q[ts]['last_price']) if q and ts in q else None
            except:
                ltp_val = None
            if ltp_val is None:
                try:
                    tm = {'NIFTY': 256265, 'BANKNIFTY': 260105, 'INDIA VIX': 264969}
                    sn = label if label != 'VIX' else 'INDIA VIX'
                    if sn in tm:
                        _hist_limiter.wait(f"index hist {label}")
                        data = kite.historical_data(tm[sn], datetime.now() - timedelta(minutes=5), datetime.now(), 'minute')
                        if data:
                            ltp_val = float(data[-1]['close'])
                except:
                    pass
            out.append({'label': label, 'ltp': ltp_val, 'kind': kind})
        return jsonify({'indices': out})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})


# ==================== UI HELPERS ====================
def gen_paper_tab(pe):
    smry = pe.summary()
    wallet = smry['wallet']
    available = smry['available']
    used_margin = smry['used_margin']
    used_percent = smry['used_percent']
    total_pnl = smry['total_pnl']
    realized = smry['realized_pnl']
    charges_paid = smry['total_charges_paid']
    pnl_color = '#00e676' if total_pnl >= 0 else '#ff1744'
    wallet_box = (
        '<div class="wallet-box" id="walletBox">'
        '<div style="flex:1;min-width:0">'
        '<div class="wallet-amt" id="wbWallet">₹' + format(int(wallet), ',') + '</div>'
        '<div class="wallet-avail" id="wbAvail">Available: ₹' + format(int(available), ',') +
        '  ·  Used: ₹' + format(int(used_margin), ',') + ' (' + str(used_percent) + '%)' +
        '  ·  Realized: <span style="color:' + ('#00e676' if realized >= 0 else '#ff1744') + '">' +
        ('+' if realized >= 0 else '') + str(round(realized, 2)) + '</span>'
        '  ·  <span style="color:var(--red);font-size:10px">Charges: ₹' + str(round(charges_paid, 2)) + '</span></div>'
        '</div>'
        '<div style="display:flex;flex-direction:column;gap:4px;min-width:0">'
        '<div id="wbTotalPnl" style="font-family:Space Mono,monospace;font-size:16px;font-weight:700;color:' + pnl_color + '">'
        + ('+' if total_pnl >= 0 else '') + '₹' + str(abs(round(total_pnl, 2))) +
        '</div><div style="font-size:9px;color:var(--text3)">NET P&L</div>'
        '</div>'
        '<div class="wallet-edit" style="display:flex;gap:6px;align-items:center;flex-wrap:wrap;width:100%">'
        '<input type="number" id="walletInput" value="' + str(int(wallet)) + '" step="10000" min="1000" style="flex:1;min-width:0">'
        '<button class="btn btn-gold" onclick="saveWallet()"><i class="fas fa-coins"></i> Set</button>'
        '</div></div>'
    )
    mode_label = 'CNC/Delivery' if pe.trading_mode == 'DELIVERY' else 'Intraday'
    # SqOff time and the 9:15-14:30 trade-slot window are INTRADAY-only
    # concepts (they exist to guarantee time to exit before the 15:15
    # square-off). CNC/Delivery has no forced exit, so the banner must not
    # claim either applies to it.
    if pe.trading_mode == 'DELIVERY':
        mode_window_txt = 'No forced square-off · Entries allowed 9:18–15:30'
    else:
        mode_window_txt = 'SqOff 15:15 · Trading Window: 9:15–14:30 ⭐⭐⭐⭐⭐'
    banner = (
        '<div class="pt-banner">'
        '<div style="font-size:20px;color:var(--gold);flex-shrink:0;padding-top:2px"><i class="fas fa-robot"></i></div>'
        '<div style="flex:1;min-width:0">'
        '<div style="font-family:Space Mono,monospace;font-weight:700;font-size:12px;color:var(--gold)">PAPER TRADING v9.8 — Strategy-Agnostic Scoring</div>'
        '<div style="font-size:11px;color:var(--text3);margin-top:2px;line-height:1.5" id="bannerModeLine">'
        '80% wallet · ATR risk sizing (1%/trade) · [' + mode_label + '] Target +' + str(round(pe.target_pct*100, 2)) + '% · SL -' + str(round(pe.stoploss_pct*100, 2)) + '% (Settings → Target &amp; Stop Loss) · ' + mode_window_txt + ' · '
        'Score≥35 · Vote≥50% · Vol≥1.3×'
        '</div></div>'
        '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px;width:100%">'
        '<span class="b bg-gold" id="bannerUniverseBadge"><i class="fas fa-chart-line"></i> ' + str(len(NIFTY200_SYMBOLS)) + ' NIFTY 200</span>'
        '<span class="b ' + ('bb' if smry['open_positions'] > 0 else 'bn') + '" id="bannerOpenBadge">' + str(smry['open_positions']) + ' Open</span>'
        '<span class="b bb" id="bannerWinBadge">' + str(smry['win_trades']) + ' ✅</span>'
        '<span class="b bs" id="bannerLossBadge">' + str(smry['loss_trades']) + ' ❌</span>'
        '<span class="b bn" id="bannerWinRateBadge">Win Rate: ' + str(smry['win_rate']) + '%</span>'
        '</div></div>'
    )
    tabs = (
        '<div class="tabs2">'
        '<div class="tab2 active" data-tab="overview" onclick="ptSwitchTab(\'overview\')">Overview</div>'
        '<div class="tab2" data-tab="positions" onclick="ptSwitchTab(\'positions\')">Positions</div>'
        '<div class="tab2" data-tab="orders" onclick="ptSwitchTab(\'orders\')">Orders</div>'
        '<div class="tab2" data-tab="trades" onclick="ptSwitchTab(\'trades\')">Trades</div>'
        '<div class="tab2" data-tab="daily" onclick="ptSwitchTab(\'daily\')">Daily</div>'
        '<div class="tab2" data-tab="siglog" onclick="ptSwitchTab(\'siglog\')">📊 Signal Log</div>'
        '<div class="tab2" data-tab="backtest" onclick="ptSwitchTab(\'backtest\')">📈 Backtest</div>'
        '<div class="tab2" data-tab="settings" onclick="ptSwitchTab(\'settings\')">⚙️ Settings</div>'
        '</div>'
        '<div id="ptTabContent"><div class="es"><div class="spin"></div><p style="margin-top:10px">Loading...</p></div></div>'
    )
    mkt_clock = '<div class="mkt-clock" id="mktClockWrap"></div>'
    return mkt_clock + banner + wallet_box + '<div id="ptCards"></div>' + tabs

# ==================== GLOBALS ====================
SYMBOL_MAP = {}

# ==================== MAIN ====================
def main():
    UserManager.load_users()
    print("\n" + "=" * 65)
    print("  ALPHA SCANNER PRO  v9.8  — Strategy-Agnostic Scoring")
    print("=" * 65)
    print("  Visit http://<your-vps-ip>:5000 to login")
    print("  Redirect URL must be set to https://rahulintratrading.online/api/broker/callback")
    print("=" * 65)
    print("  🧠 Available strategies: " + ", ".join(AVAILABLE_STRATEGIES.keys()))
    print("  📈 All strategies use pure vote-based scoring (no strategy-specific panels)")
    print("  📊 Signal logs auto‑delete after 5000 entries.")
    print("=" * 65)
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)

if __name__ == "__main__":
    main()