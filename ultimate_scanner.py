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

PRIORITY_SYMBOLS = [
    'RELIANCE','TCS','HDFCBANK','INFY','ICICIBANK','HINDUNILVR','ITC','KOTAKBANK',
    'SBIN','BHARTIARTL','LT','WIPRO','HCLTECH','AXISBANK','ASIANPAINT','MARUTI',
    'SUNPHARMA','TITAN','BAJFINANCE','NESTLEIND','POWERGRID','NTPC','ULTRACEMCO',
    'TECHM','INDUSINDBK','TATAMOTORS','BAJAJFINSV','ONGC','JSWSTEEL','HINDALCO',
    'TATASTEEL','ADANIPORTS','COALINDIA','DRREDDY','DIVISLAB','CIPLA','EICHERMOT',
    'HEROMOTOCO','GRASIM','BRITANNIA','BPCL','SHREECEM','M&M','APOLLOHOSP',
    'BAJAJ-AUTO','ADANIENT','SBILIFE','HDFCLIFE','TATACONSUM','UPL',
    'HAVELLS','PIDILITIND','BERGEPAINT','TORNTPHARM','MUTHOOTFIN','CHOLAFIN',
    'PERSISTENT','LTIM','COFORGE','OFSS','MPHASIS','ZOMATO','NYKAA','DMART',
    'INDIGO','IRCTC','PNB','BANKBARODA','CANBK','UNIONBANK','IDFCFIRSTB',
    'FEDERALBNK','BANDHANBNK','ABCAPITAL','PNBHOUSING','MANAPPURAM',
    'AUROPHARMA','LUPIN','BIOCON','ALKEM','IPCALAB','LALPATHLAB',
    'GODREJCP','DABUR','EMAMILTD','MARICO','COLPAL','VBL','JUBLFOOD',
    'PAGEIND','OBEROIRLTY','DLF','GODREJPROP','PRESTIGE','BRIGADE',
    'VOLTAS','WHIRLPOOL','POLYCAB','KPITTECH','ZEEL','SUNTV',
    'TATAPOWER','ADANIGREEN','ADANIENT','TORNTPOWER','CESC','NTPC',
    'SAIL','NMDC','HINDZINC','NATIONALUM','VEDL','JINDALSTEL',
    'ASTRAL','AARTIIND','DEEPAKNITRITE','NAVINFLUOR','SRF','BALRAMCHIN',
    'MOTHERSON','BOSCHLTD','BHARATFORG','APOLLOTYRE','BALKRISIND','MRF',
    'MCDOWELL-N','UBL','RADICO','GLOBALHEALTH','MAXHEALTH','FORTIS',
    'TRENT','NAUKRI','INFOEDGE','JUSTDIAL','IXIGO','POLICYBZR',
    'PAYTM','CARTRADE','EASEMYTRIP','RATEGAIN','SBICARD',
]

NIFTY200_SYMBOLS = [
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
    _scanners = {}
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
            scanner = cls.get_scanner(user_id)
            pe = PaperTradingEngine(config, strategies_dict, scanner=scanner, trading_mode=trading_mode,
                                     strategy_name=strategy_name, risk_config=risk_config)
            cls._paper_engines[user_id] = pe
            pe.start(cls.get_kite(user_id))
        return cls._paper_engines[user_id]

    @classmethod
    def get_scanner(cls, user_id):
        if user_id not in cls._scanners:
            config = Config(user_id)
            cls._scanners[user_id] = FullScanner(config)
        return cls._scanners[user_id]

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

# ==================== FULL SCANNER ENGINE ====================
class FullScanner:
    MIN_PRICE = 50.0
    MIN_ADX = 15.0
    MIN_SCORE = 25.0
    RATE_SLEEP = 0.67
    
    def __init__(self, config):
        self.config = config
        self.SCAN_FILE = config.SCAN_FILE
        self._lock = threading.RLock()
        self._running = False
        self._progress = {
            "status": "idle",
            "done": 0,
            "total": 0,
            "current": "",
            "results": [],
            "last_scan": None,
            "elapsed": 0,
            "errors": 0,
            "scan_min_score": 25.0,
        }
    
    @property
    def progress(self):
        with self._lock:
            return dict(self._progress)
    
    def _update(self, **kw):
        with self._lock:
            self._progress.update(kw)
    
    def _build_candidates(self, mode='priority', symbol_map=None):
        try:
            if symbol_map is None:
                return []
            if mode == 'nifty200':
                return [symbol_map[s] for s in NIFTY200_SYMBOLS if s in symbol_map]
            elif mode == 'priority':
                out = []
                for sym in PRIORITY_SYMBOLS:
                    if sym in symbol_map:
                        out.append(symbol_map[sym])
                pri_set = {s['symbol'] for s in out}
                for s in symbol_map.values():
                    if s['symbol'] not in pri_set:
                        out.append(s)
                return out
            elif mode == 'top200':
                return [symbol_map[s] for s in PRIORITY_SYMBOLS[:200] if s in symbol_map]
            elif mode == 'nifty_indices':
                return [symbol_map[s] for s in PRIORITY_SYMBOLS[:150] if s in symbol_map]
            else:
                out = []
                pri_set = set(PRIORITY_SYMBOLS)
                for sym in PRIORITY_SYMBOLS:
                    if sym in symbol_map:
                        out.append(symbol_map[sym])
                for s in symbol_map.values():
                    if s['symbol'] not in pri_set:
                        out.append(s)
                return out
        except Exception as e:
            logger.error(f"Build candidates error: {e}")
            return []
    
    def _score_stock(self, sym_info, kite, paper_engine, min_score=None, scan_end=None, scan_start5=None, scan_start15=None):
        threshold = float(min_score) if min_score is not None else self.MIN_SCORE
        sym = sym_info["symbol"]
        token = sym_info["token"]

        try:
            end = scan_end or datetime.now()
            start5 = scan_start5 or (end - timedelta(days=7))
            start15_dt = scan_start15 or (end - timedelta(days=14))

            min_bars_needed = _get_strategy_min_bars(paper_engine.strategies_dict)

            _hist_limiter.wait(f"3min {sym}")
            data5 = kite.historical_data(token, start5, end, "3minute")
            if not data5 or len(data5) < min_bars_needed:
                return None

            df5 = pd.DataFrame(data5)
            ltp = float(df5["close"].iloc[-1])
            if ltp < self.MIN_PRICE:
                return None

            avg_vol = float(df5["volume"].iloc[-20:].mean())
            if avg_vol < 10000:
                return None

            ind5 = Indicators.calculate_all(df5)
            df5_w = df5.iloc[-min_bars_needed:].reset_index(drop=True)
            ind5_w = ind5.iloc[-min_bars_needed:].reset_index(drop=True)

            strat5_b, strat5_s, _ = _strat_votes(df5_w, ind5_w, paper_engine.strategies_dict, paper_engine.strategy_performance)
            tot5 = strat5_b + strat5_s
            buy5_pct = strat5_b / tot5 * 100 if tot5 > 0 else 50.0
            sel5_pct = strat5_s / tot5 * 100 if tot5 > 0 else 50.0

            _hist_limiter.wait(f"15min {sym}")
            data15 = kite.historical_data(token, start15_dt, end, "15minute")

            htf_buy_score = htf_sell_score = 0.0
            htf_adx = 0.0
            htf_rsi = 50.0
            htf_align = False

            if data15 and len(data15) >= min_bars_needed:
                df15 = pd.DataFrame(data15)
                ind15 = Indicators.calculate_all(df15)
                df15_w = df15.iloc[-min_bars_needed:].reset_index(drop=True)
                ind15_w = ind15.iloc[-min_bars_needed:].reset_index(drop=True)
                htf_strat_b, htf_strat_s, _ = _strat_votes(df15_w, ind15_w, paper_engine.strategies_dict, paper_engine.strategy_performance)
                tot15 = htf_strat_b + htf_strat_s
                buy15_pct = htf_strat_b / tot15 * 100 if tot15 > 0 else 50.0
                sel15_pct = htf_strat_s / tot15 * 100 if tot15 > 0 else 50.0
                htf_buy_score = buy15_pct
                htf_sell_score = sel15_pct
                raw = float(ind15["adx"].iloc[-1]) if "adx" in ind15.columns else 0
                htf_adx = 0 if np.isnan(raw) else raw
                raw = float(ind15["rsi"].iloc[-1]) if "rsi" in ind15.columns else 50
                htf_rsi = 50 if np.isnan(raw) else raw
                dir5 = "BUY" if buy5_pct >= sel5_pct else "SELL"
                dir15 = "BUY" if htf_buy_score >= htf_sell_score else "SELL"
                htf_align = (dir5 == dir15)
            else:
                htf_buy_score = buy5_pct * 0.5
                htf_sell_score = sel5_pct * 0.5

            buy_score = buy5_pct * 0.6 + htf_buy_score * 0.4
            sell_score = sel5_pct * 0.6 + htf_sell_score * 0.4
            best_score = max(buy_score, sell_score)
            direction = "BUY" if buy_score >= sell_score else "SELL"
            align_bonus = 12.0 if htf_align else -5.0

            triggered = []
            for name, func in paper_engine.strategies_dict.items():
                try:
                    if func(df5_w, ind5_w):
                        triggered.append(name)
                except Exception:
                    pass

            adx_val = float(ind5["adx"].iloc[-1]) if "adx" in ind5.columns else 0
            if np.isnan(adx_val):
                adx_val = 0

            s_dir_pct = buy5_pct if direction == "BUY" else sel5_pct

            composite = round(
                best_score * 0.45
                + len(triggered) * 4
                + (adx_val - 20) * 0.3
                + s_dir_pct * 0.1
                + align_bonus
                + (htf_adx - 20) * 0.15,
                1,
            )

            if composite < threshold:
                return None
            if adx_val < self.MIN_ADX:
                return None
            if direction == "SELL" and getattr(paper_engine, 'trading_mode', 'INTRADAY') == 'DELIVERY':
                return None

            if direction == "BUY":
                rec = "STRONG BUY" if buy_score >= 80 else "BUY" if buy_score >= 55 else "NEUTRAL"
            else:
                rec = "STRONG SELL" if sell_score >= 80 else "SELL" if sell_score >= 55 else "NEUTRAL"

            rsi_val = float(ind5["rsi"].iloc[-1]) if "rsi" in ind5.columns else 50
            if np.isnan(rsi_val):
                rsi_val = 50

            try:
                today_str = end.strftime("%Y-%m-%d")
                dt_s = pd.to_datetime(df5["date"] if "date" in df5.columns else df5.index)
                yd_bars = df5[dt_s.dt.strftime("%Y-%m-%d") < today_str]
                prev_close = (
                    float(yd_bars["close"].iloc[-1])
                    if len(yd_bars) > 0
                    else float(df5["close"].iloc[0])
                )
            except Exception:
                prev_close = float(df5["close"].iloc[-2]) if len(df5) > 1 else ltp

            change_pct = round((ltp - prev_close) / prev_close * 100, 2) if prev_close else 0
            gap_pct = change_pct
            vol_ratio = round(float(df5["volume"].iloc[-1]) / (avg_vol + 1e-9), 2)

            if vol_ratio > 2.0:
                composite = round(composite + 5.0, 1)
            elif vol_ratio > 1.5:
                composite = round(composite + 2.0, 1)
            if abs(gap_pct) > 1.0 and vol_ratio > 1.3:
                composite = round(composite + 3.0, 1)

            return {
                "symbol": sym,
                "price": round(ltp, 2),
                "change": change_pct,
                "volume": int(df5["volume"].iloc[-1]),
                "avg_volume": int(avg_vol),
                "buy_score": round(buy_score, 1),
                "sell_score": round(sell_score, 1),
                "buy5_score": round(buy5_pct, 1),
                "sell5_score": round(sel5_pct, 1),
                "buy15_score": round(htf_buy_score, 1),
                "sell15_score": round(htf_sell_score, 1),
                "htf_align": htf_align,
                "composite_score": composite,
                "direction": direction,
                "recommendation": rec,
                "buy_pct": round(buy5_pct, 1),
                "sell_pct": round(sel5_pct, 1),
                "signal_count": len(triggered),
                "strategies": triggered[:8],
                "indicators": {
                    "rsi": round(rsi_val, 1),
                    "adx": round(adx_val, 1),
                    "htf_rsi": round(htf_rsi, 1),
                    "htf_adx": round(htf_adx, 1),
                    "atr_pct": round(
                        float(ind5["atr_percent"].iloc[-1])
                        if "atr_percent" in ind5.columns else 0, 2
                    ),
                },
                "htf_bull": round(
                    float(ind5["htf_bull"].iloc[-1])
                    if "htf_bull" in ind5.columns else 0.5, 2
                ),
                "already_pinned": sym in paper_engine.data.get("pinned", []),
                "gap_pct": round(gap_pct, 2),
                "vol_ratio": round(vol_ratio, 2),
            }

        except Exception as e:
            logger.debug(f"Score stock error {sym}: {e}")
            return None
    
    def run_scan(self, kite, paper_engine, symbol_map, mode="all", max_stocks=None, min_score=None):
        if self._running:
            return {"status": "already_running"}
 
        scan_min_score = float(min_score) if min_score is not None else self.MIN_SCORE
 
        def _worker():
            self._running = True
            t0 = time.time()
            try:
                candidates = self._build_candidates(mode, symbol_map)
                if max_stocks:
                    candidates = candidates[: int(max_stocks)]
 
                _now = datetime.now()
                _mins = (_now.minute // 5) * 5
                scan_end = _now.replace(minute=_mins, second=0, microsecond=0)
                if (_now - scan_end).total_seconds() < 3:
                    scan_end -= timedelta(minutes=5)
 
                scan_start5 = scan_end - timedelta(days=7)
                scan_start15 = scan_end - timedelta(days=14)
 
                logger.info(
                    f"Scanner locked to {scan_end.strftime('%Y-%m-%d %H:%M')} "
                    f"for {len(candidates)} stocks  |  RATE_SLEEP={self.RATE_SLEEP}s"
                )
 
                self._update(
                    status="running",
                    done=0,
                    total=len(candidates),
                    results=[],
                    errors=0,
                    current="Starting...",
                    scan_min_score=scan_min_score,
                )
 
                results = []
                errors = 0
 
                for idx, sym_info in enumerate(candidates):
                    if not self._running:
                        break
 
                    sym = sym_info["symbol"]
                    self._update(done=idx, current=sym)
 
                    try:
                        res = self._score_stock(
                            sym_info,
                            kite,
                            paper_engine,
                            scan_min_score,
                            scan_end,
                            scan_start5,
                            scan_start15,
                        )
                        if res:
                            results.append(res)
                            self._update(results=list(results))
                    except Exception as e:
                        errors += 1
                        logger.error(f"Scan error {sym}: {e}")
 
                    self._update(errors=errors)
                    time.sleep(self.RATE_SLEEP)
 
                priority_order = {
                    "STRONG BUY": 0, "STRONG SELL": 1,
                    "BUY": 2, "SELL": 3,
                    "NEUTRAL": 4,
                }
                results.sort(
                    key=lambda x: (
                        priority_order.get(x["recommendation"], 5),
                        -x["composite_score"],
                    )
                )
 
                elapsed = round(time.time() - t0, 1)
                self._update(
                    status="done",
                    done=len(candidates),
                    total=len(candidates),
                    results=results,
                    errors=errors,
                    last_scan=datetime.now().isoformat(),
                    elapsed=elapsed,
                    current=f"Done — {len(results)} candidates found",
                )
 
                try:
                    scan_out = {}
                    for r in results:
                        for s in r.get("strategies", []):
                            if s not in scan_out:
                                scan_out[s] = []
                            scan_out[s].append(r)
                    with open(self.SCAN_FILE, "w") as f:
                        json.dump(scan_out, f, indent=2, cls=DateTimeEncoder)
                except Exception as e:
                    logger.error(f"Scan save error: {e}")
 
                logger.info(
                    f"Scan complete: {len(results)} candidates in {elapsed}s, "
                    f"{errors} errors"
                )
 
            except Exception as e:
                logger.error(f"Scan worker error: {e}")
            finally:
                self._running = False
 
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return {"status": "started"}
    
    def stop_scan(self):
        self._running = False
    
    def get_results(self, limit=100, min_score=None):
        with self._lock:
            results = list(self._progress.get('results', []))
            stored_min = self._progress.get('scan_min_score', self.MIN_SCORE)
        effective_min = float(min_score) if min_score is not None else stored_min
        results = [r for r in results if r['composite_score'] >= effective_min]
        return results[:limit]

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
    
    def __init__(self, config, strategies_dict, scanner=None, trading_mode='INTRADAY', strategy_name=None, risk_config=None):
        self.config = config
        self.strategies_dict = strategies_dict
        self.scanner = scanner
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
        permanently-unqualified pinned stock can't flood the signal log every
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

        if atr and atr > 0:
            risk_amount = wallet * self.RISK_PER_TRADE_PCT
            sl_distance = atr * self.ATR_SL_MULTIPLIER
            if sl_distance > 0:
                risk_qty = int(risk_amount / sl_distance)
                qty = max(1, min(margin_qty, risk_qty))
                logger.info(
                    f"Risk sizing [{symbol}]: ₹{wallet:.0f}×{self.RISK_PER_TRADE_PCT*100:.0f}%"
                    f"=₹{risk_amount:.0f} ÷ SL({sl_distance:.2f}) → risk_qty={risk_qty}"
                    f" | margin_cap={margin_qty} | final={qty}"
                )
            else:
                qty = margin_qty
        else:
            qty = margin_qty

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

            category = AVAILABLE_STRATEGY_META.get(strategy_name, {}).get('category', 'default')
            skip_extension_checks = category in ('breakout', 'momentum')

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
        # during market hours (already gated to 9:30-15:30 by the caller in
        # _check_signal via MARKET_OPEN+15 / MARKET_CLOSE).
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
        try:
            if df is not None and len(df) > 20 and 'date' in df.columns:
                today_str = datetime.now().strftime('%Y-%m-%d')
                today_mask = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d') == today_str
                yesterday_prices = df[~today_mask]['close']
                if len(yesterday_prices) > 0:
                    prev_close = float(yesterday_prices.iloc[-1])
                    gap_pct = (price - prev_close) / prev_close * 100
                    if side == 'BUY' and gap_pct > 1.5:
                        _block(f"Gap-up filter: +{gap_pct:.1f}% from prev close (limit +1.5%)")
                        return None
                    if side == 'SELL' and gap_pct < -1.5:
                        _block(f"Gap-down filter: {gap_pct:.1f}% from prev close (limit -1.5%)")
                        return None
        except Exception:
            pass
        if df is not None and ind is not None:
            ok, quality_reason = self._check_entry_quality(df, ind, side, symbol, strategy_name)
            if not ok:
                _block(f"Quality check failed: {quality_reason}")
                return None
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
    
    def pin(self, symbol, direction=None, recommendation=None, score=None):
        with self._lock:
            if symbol not in self.data['pinned']:
                self.data['pinned'].append(symbol)
            if 'pinned_meta' not in self.data:
                self.data['pinned_meta'] = {}
            def _clean(v):
                if v is None:
                    return None
                s = str(v).strip()
                return None if s.lower() in ('undefined', 'null', '') else s
            direction = _clean(direction)
            recommendation = _clean(recommendation)
            meta = {}
            if direction:
                meta['direction'] = direction.upper()
            if recommendation:
                meta['recommendation'] = recommendation
            if score is not None:
                try:
                    meta['score'] = float(score)
                except:
                    pass
            if not meta.get('direction'):
                meta['direction'] = 'BOTH'
            self.data['pinned_meta'][symbol] = meta
            self._save()
    
    def unpin(self, symbol):
        with self._lock:
            if symbol in self.data['pinned']:
                self.data['pinned'].remove(symbol)
                self.data.get('pinned_meta', {}).pop(symbol, None)
                self._save()
    
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
                if self.MARKET_OPEN <= mins < self.MARKET_OPEN + 15:
                    return
                if mins >= self.MARKET_CLOSE:
                    return
            elif mins >= self.MARKET_CLOSE:
                return

            _current_slot_ok = _in_trade_slot(mins)
            _current_slot_lbl = _slot_label(mins)

            min_bars_needed = _get_strategy_min_bars(self.strategies_dict)

            _hist_limiter.wait(f"3min {symbol}")
            data5 = kite.historical_data(
                SYMBOL_MAP[symbol]["token"],
                now - timedelta(days=6),
                now,
                "3minute",
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
            vol_mult = 1.0 if now_mins < 10 * 60 else 0.5 if now_mins < 13 * 60 else 0.4

            if cur_vol < avg_vol * vol_mult:
                self._add_signal_log(
                    {
                        "time": now.strftime("%H:%M:%S"),
                        "date": now.strftime("%Y-%m-%d"),
                        "symbol": symbol,
                        "ltp": round(ltp_initial, 2),
                        "volume": cur_vol,
                        "avg_volume": int(avg_vol),
                        "status": "REJECTED",
                        "reason": (
                            f"Low volume {cur_vol:,} < "
                            f"{int(avg_vol*vol_mult):,} ({vol_mult}× avg)"
                        ),
                    }
                )
                return

            pin_meta = self.data.get("pinned_meta", {}).get(symbol, {})
            pin_dir = pin_meta.get("direction", "BOTH")
            allow_buy = pin_dir in ("BUY", "BOTH")
            allow_sell = pin_dir in ("SELL", "BOTH")

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

            buy_ok = (
                allow_buy
                and s5_buy_pct >= self.MIN_VOTE_PCT
                and strat5_b >= 1.0
            )
            sell_ok = (
                allow_sell
                and s5_sel_pct >= self.MIN_VOTE_PCT
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

            skip_breakout = (
                market_regime == "RANGING"
                and best_strategy
                and AVAILABLE_STRATEGY_META.get(best_strategy, {}).get('category') == 'breakout'
            )

            log_entry = {
                "time": now.strftime("%H:%M:%S"),
                "date": now.strftime("%Y-%m-%d"),
                "symbol": symbol,
                "ltp": round(ltp_initial, 2),
                "volume": int(df["volume"].iloc[-1]),
                "avg_volume": int(avg_vol),
                "buy_score": round(buy_score, 1),
                "sell_score": round(sell_score, 1),
                "vote_b": round(s5_buy_pct, 1),
                "vote_s": round(s5_sel_pct, 1),
                "htf15_buy_pct": round(htf15_buy_pct, 1),
                "htf15_sel_pct": round(htf15_sel_pct, 1),
                "hard_buy": buy_ok,
                "hard_sell": sell_ok,
                "htf_bull": round(htf_bull, 3),
                "strategies": direction_triggered[:5],
                "all_strategies": all_triggered[:8],
                "pin_dir": pin_dir,
                "pin_rec": pin_meta.get("recommendation", ""),
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

                def _corr_ok(trade_side):
                    if not nifty_data:
                        return True
                    if market_trend == "BULLISH" and trade_side == "SELL" and symbol in HEAVYWEIGHTS:
                        return False
                    if market_trend == "BEARISH" and trade_side == "BUY" and symbol in HEAVYWEIGHTS:
                        return False
                    return True

                if buy_ok and not _corr_ok("BUY"):
                    log_entry["status"] = "REJECTED"
                    log_entry["reason"] = (
                        f"Correlation block: market {market_trend}, "
                        f"cannot BUY heavyweight {symbol}"
                    )
                    self._add_signal_log(log_entry)
                    return

                if sell_ok and not _corr_ok("SELL"):
                    log_entry["status"] = "REJECTED"
                    log_entry["reason"] = (
                        f"Correlation block: market {market_trend}, "
                        f"cannot SELL heavyweight {symbol}"
                    )
                    self._add_signal_log(log_entry)
                    return

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
                        f"HTF: {round(htf_bull,3)} | Pinned as: {pin_dir} | "
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
                        f"HTF: {round(htf_bull,3)} | Pinned as: {pin_dir} | "
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
                    raw_buy_ok = (s5_buy_pct >= self.MIN_VOTE_PCT and strat5_b >= 1.0)
                    raw_sell_ok = (s5_sel_pct >= self.MIN_VOTE_PCT and strat5_s >= 1.0)
                    if raw_buy_ok and not allow_buy:
                        reasons.append(f"BUY signal blocked — stock pinned as {pin_dir} only")
                    if raw_sell_ok and not allow_sell:
                        reasons.append(f"SELL signal blocked — stock pinned as {pin_dir} only")

                    leaning_buy = buy_score >= sell_score
                    check_dir = (
                        "BUY" if (pin_dir == "BOTH" and leaning_buy)
                        else pin_dir if pin_dir != "BOTH"
                        else ("BUY" if leaning_buy else "SELL")
                    )
                    if check_dir == "BUY":
                        if s5_buy_pct < self.MIN_VOTE_PCT:
                            reasons.append(f"5m buy vote {s5_buy_pct:.0f}% < {self.MIN_VOTE_PCT:.0f}%")
                        if strat5_b < 1.0:
                            reasons.append(f"No {self.strategy_name} BUY strategy triggered")
                    else:
                        if s5_sel_pct < self.MIN_VOTE_PCT:
                            reasons.append(f"5m sell vote {s5_sel_pct:.0f}% < {self.MIN_VOTE_PCT:.0f}%")
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
                        pinned = list(self.data.get("pinned", []))
                        positions = list(self.data.get("positions", {}).keys())
                    all_syms = list(set(pinned + positions))
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
                        with self._lock:
                            logger.info(f"Monitoring {len(pinned)} pinned + {len(positions)} positions  |  batch_ltp covered {len(all_syms)} symbols")
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
            pinned = list(self.data.get('pinned', []))
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
            'pinned_count': len(pinned),
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
            'pinned_meta': dict(self.data.get('pinned_meta', {})),
        }
    
    def _scan_for_pinnable_stocks(self, max_results=50):
        ACTIONABLE = {'STRONG BUY', 'BUY', 'STRONG SELL', 'SELL'}
        live_results = self.scanner.get_results(limit=500)
        if live_results:
            pinned = set(self.data.get('pinned', []))
            filtered = []
            for r in live_results:
                r['already_pinned'] = r['symbol'] in pinned
                if r.get('recommendation', 'NEUTRAL') in ACTIONABLE:
                    filtered.append(r)
            return filtered[:max_results]
        scan_file = self.config.SCAN_FILE
        if not os.path.exists(scan_file):
            return []
        try:
            with open(scan_file, 'r') as f:
                scan_results = json.load(f)
            candidates = {}
            for strat, stocks in scan_results.items():
                for s in stocks:
                    sym = s.get('symbol', '')
                    price = s.get('price', 0)
                    if price < self.MIN_PRICE:
                        continue
                    if sym not in candidates:
                        candidates[sym] = {
                            'symbol': sym,
                            'price': price,
                            'change': s.get('change', 0),
                            'volume': s.get('volume', 0),
                            'strategies': list(s.get('strategies', [])),
                            'indicators': s.get('indicators', {}),
                            'signal_count': s.get('signal_count', 0),
                            'recommendation': s.get('recommendation', 'NEUTRAL'),
                            'buy_pct': s.get('buy_pct', 50),
                            'sell_pct': s.get('sell_pct', 50),
                            'composite_score': s.get('composite_score', 0),
                        }
                    else:
                        for st in s.get('strategies', []):
                            if st not in candidates[sym]['strategies']:
                                candidates[sym]['strategies'].append(st)
                        candidates[sym]['signal_count'] = len(candidates[sym]['strategies'])
            already_pinned = set(self.data.get('pinned', []))
            results = []
            for sym, c in candidates.items():
                ind = c['indicators']
                rsi = ind.get('rsi', 50)
                adx = ind.get('adx', 0)
                if adx < 15 or c['price'] < self.MIN_PRICE:
                    continue
                if c['recommendation'] not in ACTIONABLE:
                    continue
                results.append({
                    'symbol': sym,
                    'price': round(c['price'], 2),
                    'change': round(c['change'], 2),
                    'volume': c['volume'],
                    'signal_count': c['signal_count'],
                    'recommendation': c['recommendation'],
                    'buy_pct': c['buy_pct'],
                    'sell_pct': c['sell_pct'],
                    'rsi': round(rsi, 1) if rsi else None,
                    'adx': round(adx, 1) if adx else None,
                    'strategies': c['strategies'][:5],
                    'composite_score': round(c.get('composite_score', 0), 1),
                    'already_pinned': sym in already_pinned,
                })
            results.sort(key=lambda x: (
                0 if x['recommendation'] in ['STRONG BUY', 'STRONG SELL'] else 1,
                -x['composite_score']
            ))
            return results[:max_results]
        except Exception as e:
            logger.error(f"Scan pinnable error: {e}")
            return []

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
        # across several pinned stocks this routinely takes well past a
        # minute. Running it synchronously inside the Flask request means
        # nginx's proxy_read_timeout (commonly 60s) fires first and the
        # client sees a 504, even though the backend was still working fine.
        # run_async() below runs it in a background thread instead; the
        # route returns immediately and the client polls /api/backtest/status
        # (same pattern as FullScanner.run_scan / /scanner/status).
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

    def run_async(self, kite, symbol_map, pinned_stocks, initial_wallet=100000, from_date=None, to_date=None):
        if self._running:
            return {'status': 'already_running'}
        self._running = True
        self._update(status='running', done=0, total=len(pinned_stocks),
                     current='Starting...', results=None, error=None)

        def _worker():
            try:
                results = self.run(
                    kite, symbol_map, pinned_stocks,
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

    def run(self, kite, symbol_map, pinned_stocks, initial_wallet=100000, from_date=None, to_date=None, progress_cb=None):
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
        logger.info(f"BACKTEST START: {len(pinned_stocks)} stocks, {len(self.strategies_dict)} strategies")
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

        total_syms = len(pinned_stocks)
        # Mirrors live's _check_signal() buy_ok/sell_ok gate exactly: a
        # direction only qualifies if its share of triggered-strategy points
        # is >= MIN_VOTE_PCT (50%) AND at least one point actually triggered
        # in that direction. The old raw point-count threshold
        # (max(2, 30% of strategy count)) had no percentage comparison at
        # all, so a backtest "signal" wasn't necessarily something live
        # would ever fire — this made backtest results not a reliable
        # preview of live behavior.
        min_vote_pct = PaperTradingEngine.MIN_VOTE_PCT
        logger.info(f"Vote gate: >= {min_vote_pct:.0f}% of triggered points in one direction (matches live _check_signal)")

        min_bars_needed = _get_strategy_min_bars(self.strategies_dict)
        logger.info(f"Min-bar gate set to {min_bars_needed}")

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
            logger.info(f"NIFTY context loaded: {len(nifty_ctx)} bars for regime/correlation gating")
        else:
            logger.warning("NIFTY context unavailable — regime/correlation gates disabled for this run")

        symbol_data = {}
        for _sym_idx, sym in enumerate(pinned_stocks):
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
                data = kite.historical_data(token, start, end, "3minute")
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
            # mode — refuses to evaluate a *new* entry in the first 15
            # minutes of the session (9:15-9:30, so indicators/opening
            # range have bars to form) or once the market has closed:
            #   if MARKET_OPEN <= mins < MARKET_OPEN+15: return
            #   if mins >= MARKET_CLOSE: return
            # Previously this whole window check was nested inside
            # `self.trading_mode == 'INTRADAY'`, so a Delivery/CNC backtest
            # could open a position at 9:16 AM or 3:29 PM — something live
            # would always refuse no matter the mode.
            if bar_mins < PaperTradingEngine.MARKET_OPEN + 15 or bar_mins >= PaperTradingEngine.MARKET_CLOSE:
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
                    f"b={b:.1f} ({buy_pct:.0f}%)  s={s:.1f} ({sell_pct:.0f}%)  (gate={min_vote_pct:.0f}%)"
                )
                vote_log_count[sym] = vlc + 1

            # Same formula as live's buy_ok/sell_ok in _check_signal():
            # percentage of triggered points >= MIN_VOTE_PCT, AND at least
            # one point actually triggered in that direction (b/s >= 1.0).
            buy_ok = buy_pct >= min_vote_pct and b >= 1.0
            sell_ok = (
                sell_pct >= min_vote_pct and s >= 1.0
                and self.trading_mode != 'DELIVERY'
            )
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
            # Needed so category-based checks below (breakout regime skip,
            # VWAP/RSI/EMA50 extension vetoes inside _check_entry_quality)
            # are evaluated against the strategy that actually fired,
            # instead of being skipped entirely as backtest previously did.
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

            # Regime filter — mirrors live's skip_breakout: don't take a
            # BREAKOUT-category strategy's signal (e.g. ORB) while NIFTY is
            # RANGING (ADX <= 25). Previously backtest had no NIFTY context
            # at all, so it would happily take breakout signals live's
            # _check_signal() would refuse outright.
            market_trend, market_regime = self._market_context_at(nifty_ctx, bar_dt)
            if (
                market_regime == "RANGING"
                and AVAILABLE_STRATEGY_META.get(best_strategy, {}).get('category') == 'breakout'
            ):
                logger.debug(f"  {sym} {side} skipped — market RANGING, breakout strategy {best_strategy}")
                continue

            # Correlation filter — mirrors live's _corr_ok(): don't fight
            # the index trend on a heavyweight constituent.
            if market_trend == "BULLISH" and side == "SELL" and sym in HEAVYWEIGHTS:
                continue
            if market_trend == "BEARISH" and side == "BUY" and sym in HEAVYWEIGHTS:
                continue

            gap_ok = True
            try:
                dt_slice = dt_series.iloc[:i+1]
                today_mask = (dt_slice.dt.date == bar_dt.date()).values
                yesterday_prices = df_slice.loc[~today_mask, 'close']
                if len(yesterday_prices) > 0:
                    prev_close = float(yesterday_prices.iloc[-1])
                    if prev_close > 0:
                        gap_pct = (ltp - prev_close) / prev_close * 100
                        if side == 'BUY' and gap_pct > 1.5:
                            gap_ok = False
                        if side == 'SELL' and gap_pct < -1.5:
                            gap_ok = False
            except Exception:
                gap_ok = True
            if not gap_ok:
                continue

            price = ltp
            atr = float(ind_slice['atr'].iloc[-1]) if 'atr' in ind_slice.columns else None

            # Post-vote entry-quality veto — mirrors live's
            # _open_position_nolock(), which runs every signal through
            # _check_entry_quality() (volume surge, VWAP/ATR extension,
            # RSI overbought/oversold, EMA50 distance, adverse candle
            # pattern on the trigger bar) AFTER the vote passes. This
            # method was already fully ported into BacktestEngine but was
            # never actually called, so backtest was taking every
            # vote-qualified signal live would go on to reject on quality.
            quality_ok, _quality_reason = self._check_entry_quality(df_w, ind_w, side, sym, bar_dt, best_strategy)
            if not quality_ok:
                logger.debug(f"  {sym} {side} rejected by entry quality: {_quality_reason}")
                continue

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

            # ATR-based risk sizing — mirrors live's
            # _smart_allocation_time_based(): cap qty by risk-per-trade
            # (wallet * RISK_PER_TRADE_PCT / (ATR * ATR_SL_MULTIPLIER)) in
            # addition to the plain margin-affordability cap. Previously
            # backtest only ever sized by margin affordability, which could
            # size a position far larger than live ever would on a
            # high-ATR (volatile) stock.
            if atr and atr > 0 and qty > 0:
                risk_amount = wallet * PaperTradingEngine.RISK_PER_TRADE_PCT
                sl_distance = atr * PaperTradingEngine.ATR_SL_MULTIPLIER
                if sl_distance > 0:
                    risk_qty = int(risk_amount / sl_distance)
                    qty = max(1, min(qty, risk_qty))

            # Mirrors live's _smart_allocation_time_based() slot-based sizing
            # cut — backtest was previously always sizing at full 80% wallet
            # regardless of time of day.
            
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

            category = AVAILABLE_STRATEGY_META.get(strategy_name, {}).get('category', 'default')
            skip_extension_checks = category in ('breakout', 'momentum')

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

    pinned = pe.data.get('pinned', [])
    if not pinned:
        return jsonify({'status': 'error', 'msg': 'No pinned stocks to backtest'}), 400

    existing = UserManager._backtest_engines.get(user_id)
    if existing and existing._running:
        return jsonify({'status': 'already_running'})

    symbol_map = get_symbol_map()
    # Fire-and-poll instead of blocking this request — see BacktestEngine
    # docstring for why: a wide date range can take well over nginx's
    # default proxy_read_timeout and surface as a 504 even on success.
    backtest = BacktestEngine(strategies_dict, trading_mode=mode,
                               target_pct=target_pct_ui / 100.0, stoploss_pct=stoploss_pct_ui / 100.0,
                               min_hold_days=min_hold_days,
                               strategy_performance=dict(pe.strategy_performance or {}))
    UserManager._backtest_engines[user_id] = backtest
    result = backtest.run_async(kite, symbol_map, pinned, initial_wallet=wallet, from_date=from_date, to_date=to_date)
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
    scanner = UserManager.get_scanner(user_id)
    sector = UserManager.get_sector_monitor(user_id)
    return user_id, kite, pe, scanner, sector

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
        pinned_count=len(pe.data.get('pinned', [])),
        pinned_symbols=pe.data.get('pinned', []),
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

@app.route('/paper/pin', methods=['POST'])
def paper_pin():
    try:
        _, _, pe, _, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        d = request.json
        sym = d.get('symbol', '').upper().strip()
        action = d.get('action', 'pin')
        symbol_map = get_symbol_map()
        if not sym or sym not in symbol_map:
            return jsonify({'status': 'error', 'msg': 'Symbol not found'})
        if action == 'pin':
            direction = d.get('direction')
            recommendation = d.get('recommendation')
            score = d.get('score')
            pe.pin(sym, direction=direction, recommendation=recommendation, score=score)
            dir_label = f' [{direction}]' if direction else ''
            return jsonify({'status': 'ok', 'msg': sym + ' pinned' + dir_label})
        else:
            pe.unpin(sym)
            return jsonify({'status': 'ok', 'msg': sym + ' unpinned'})
    except Exception as e:
        logger.error(f"Pin error: {e}")
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/wallet', methods=['POST'])
def paper_wallet():
    try:
        _, _, pe, _, _ = get_user_engines()
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
        _, _, pe, _, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        smry = pe.summary()
        smry['pinned_list'] = pe.data.get('pinned', [])
        return jsonify(smry)
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/margin-info')
def paper_margin_info():
    try:
        _, _, pe, _, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        pinned = pe.data.get("pinned", [])
        result = {}
        for sym in pinned:
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
        _, _, pe, _, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        return jsonify({'orders': pe.data.get('orders', [])})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/trades')
def paper_trades():
    try:
        _, _, pe, _, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        return jsonify({'trades': pe.data.get('trades', [])})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/paper/exit', methods=['POST'])
def paper_exit():
    try:
        _, _, pe, _, _ = get_user_engines()
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
        _, _, pe, _, _ = get_user_engines()
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

@app.route('/paper/scan-pinnable')
def paper_scan_pinnable():
    try:
        _, _, pe, scanner, _ = get_user_engines()
        if not pe:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        results = pe._scan_for_pinnable_stocks(max_results=100)
        p = scanner.progress
        return jsonify({
            'stocks': results,
            'count': len(results),
            'total_scanned': p.get('done', 0),
            'total_found': len(p.get('results', [])),
            'min_score': p.get('scan_min_score', scanner.MIN_SCORE),
            'last_scan': p.get('last_scan'),
        })
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/sector/status')
def sector_status():
    try:
        _, _, _, _, sector = get_user_engines()
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

@app.route('/scanner/start', methods=['POST'])
def scanner_start():
    try:
        _, kite, pe, scanner, _ = get_user_engines()
        if not pe or not kite:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        d = request.json or {}
        mode = d.get('mode', 'all')
        max_stocks = d.get('max_stocks', None)
        min_score = d.get('min_score', None)
        symbol_map = get_symbol_map()
        result = scanner.run_scan(kite, pe, symbol_map, mode=mode, max_stocks=max_stocks, min_score=min_score)
        return jsonify(result)
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/scanner/stop', methods=['POST'])
def scanner_stop():
    try:
        _, _, _, scanner, _ = get_user_engines()
        if not scanner:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        scanner.stop_scan()
        return jsonify({'status': 'stopped'})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/scanner/status')
def scanner_status():
    try:
        _, _, _, scanner, _ = get_user_engines()
        if not scanner:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        p = scanner.progress
        return jsonify({
            'status': p['status'],
            'done': p['done'],
            'total': p['total'],
            'current': p['current'],
            'found': len(p['results']),
            'errors': p['errors'],
            'last_scan': p['last_scan'],
            'elapsed': p['elapsed'],
        })
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/scanner/results')
def scanner_results():
    try:
        _, _, _, scanner, _ = get_user_engines()
        if not scanner:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        limit = int(request.args.get('limit', 100))
        min_score = request.args.get('min_score', None)
        results = scanner.get_results(limit=limit, min_score=min_score)
        pe = UserManager.get_paper_engine(session.get('user_id'))
        pinned = set(pe.data.get('pinned', []))
        for r in results:
            r['already_pinned'] = r['symbol'] in pinned
        return jsonify({'results': results, 'count': len(results)})
    except Exception as e:
        return jsonify({'status': 'error', 'msg': str(e)})

@app.route('/market-indices')
def market_indices():
    try:
        _, kite, _, _, _ = get_user_engines()
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

@app.route('/market/movers')
def market_movers():
    try:
        _, kite, _, _, _ = get_user_engines()
        if not kite:
            return jsonify({'status': 'error', 'msg': 'Not logged in'})
        symbol_map = get_symbol_map()
        candidates = [symbol_map.get(s) for s in PRIORITY_SYMBOLS[:150] if s in symbol_map]
        gainers, losers, vol_gainers, momentum = [], [], [], []
        now = datetime.now()
        # Widened lookback to reliably span holidays/long weekends
        start5 = now - timedelta(days=10)
        ohlc_map = {}

        try:
            batch_size = 100
            sym_list = [s["symbol"] for s in candidates]
            for i in range(0, len(sym_list), batch_size):
                batch = [f"NSE:{s}" for s in sym_list[i : i + batch_size]]
                _quote_limiter.wait("movers ohlc batch")
                ohlc_resp = kite.ohlc(batch)
                for key, val in ohlc_resp.items():
                    sym = key.replace("NSE:", "")
                    ohlc_map[sym] = val
        except Exception as e:
            logger.warning(f"OHLC batch fetch error: {e}")

        skipped_no_prev_close = 0

        for sym_info in candidates:
            try:
                _hist_limiter.wait(f"movers hist {sym_info['symbol']}")
                data = kite.historical_data(sym_info["token"], start5, now, "3minute")
                if not data or len(data) < 10:
                    continue

                df = pd.DataFrame(data)
                sym = sym_info["symbol"]

                today_str = now.strftime("%Y-%m-%d")
                dt_series = pd.to_datetime(df["date"])
                today_mask = dt_series.dt.strftime("%Y-%m-%d") == today_str
                yesterday_bars = df[~today_mask]

                # Require a genuine prior trading day's close. If we don't have
                # one even with a 10-day lookback, skip the stock rather than
                # fabricate a prev_close from today's own data (which would
                # produce a fake/misleading change%).
                if len(yesterday_bars) == 0:
                    skipped_no_prev_close += 1
                    continue
                prev_close = float(yesterday_bars["close"].iloc[-1])
                if prev_close <= 0:
                    skipped_no_prev_close += 1
                    continue

                # Single source of truth for LTP: prefer live OHLC quote (most
                # current), fall back to last historical candle close only if
                # the quote batch didn't return this symbol.
                ohlc_entry = ohlc_map.get(sym, {})
                if ohlc_entry.get("last_price"):
                    ltp = float(ohlc_entry["last_price"])
                else:
                    ltp = float(df["close"].iloc[-1])

                if ltp < 50:
                    continue

                change_pct = round((ltp - prev_close) / prev_close * 100, 2)

                avg_vol = float(df["volume"].iloc[-20:].mean()) if len(df) >= 20 else float(df["volume"].mean())
                cur_vol = int(df["volume"].iloc[-1])
                vol_ratio = round(cur_vol / avg_vol, 2) if avg_vol > 0 else 0

                ohlc = ohlc_entry.get("ohlc", {})
                if ohlc and ohlc.get("high") and ohlc.get("low"):
                    today_high = float(ohlc["high"])
                    today_low = float(ohlc["low"])
                else:
                    today_bars = df[today_mask]
                    if len(today_bars) > 0:
                        today_high = float(today_bars["high"].max())
                        today_low = float(today_bars["low"].min())
                    else:
                        today_high = round(ltp * 1.001, 2)
                        today_low = round(ltp * 0.999, 2)

                day_range_pct = round((today_high - today_low) / prev_close * 100, 2)

                try:
                    d = df["close"].diff()
                    g = d.where(d > 0, 0).ewm(com=13, adjust=False).mean()
                    l_s = (-d.where(d < 0, 0)).ewm(com=13, adjust=False).mean()
                    rsi = float(100 - 100 / (1 + g / l_s.clip(lower=1e-10)).iloc[-1])
                except Exception:
                    rsi = 50.0

                item = {
                    "symbol": sym,
                    "ltp": round(ltp, 2),
                    "prev_close": round(prev_close, 2),
                    "change": change_pct,
                    "vol_ratio": vol_ratio,
                    "cur_vol": cur_vol,
                    "avg_vol": int(avg_vol),
                    "today_high": round(today_high, 2),
                    "today_low": round(today_low, 2),
                    "day_range_pct": day_range_pct,
                    "rsi": round(rsi, 1),
                }

                if change_pct > 0.3: gainers.append(item)
                elif change_pct < -0.3: losers.append(item)
                if vol_ratio > 1.5: vol_gainers.append(item)
                if vol_ratio > 1.3 and abs(change_pct) > 0.3:
                    momentum.append(item)

            except Exception as e:
                logger.debug(f"Movers {sym_info['symbol']}: {e}")
                continue

        gainers.sort(key=lambda x: -x["change"])
        losers.sort(key=lambda x: x["change"])
        vol_gainers.sort(key=lambda x: -x["vol_ratio"])
        momentum.sort(key=lambda x: -(abs(x["change"]) * x["vol_ratio"]))

        if skipped_no_prev_close:
            logger.info(f"Movers: skipped {skipped_no_prev_close} symbols with no reliable prev_close")

        return jsonify({
            "gainers": gainers[:15],
            "losers": losers[:15],
            "vol_gainers": vol_gainers[:15],
            "momentum": momentum[:15],
            "as_of": now.strftime("%H:%M:%S"),
            "skipped": skipped_no_prev_close,
        })
    except Exception as e:
        logger.error(f"Movers error: {e}")
        return jsonify({"gainers": [], "losers": [], "vol_gainers": [], "momentum": [], "error": str(e)})

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
        mode_window_txt = 'No forced square-off · Entries allowed 9:30–15:30'
    else:
        mode_window_txt = 'SqOff 15:15 · Trading Window: 9:15–14:30 ⭐⭐⭐⭐⭐'
    banner = (
        '<div class="pt-banner">'
        '<div style="font-size:20px;color:var(--gold);flex-shrink:0;padding-top:2px"><i class="fas fa-robot"></i></div>'
        '<div style="flex:1;min-width:0">'
        '<div style="font-family:Space Mono,monospace;font-weight:700;font-size:12px;color:var(--gold)">PAPER TRADING v9.8 — Strategy-Agnostic Scoring</div>'
        '<div style="font-size:11px;color:var(--text3);margin-top:2px;line-height:1.5">'
        '80% wallet · ATR risk sizing (1%/trade) · [' + mode_label + '] Target +' + str(round(pe.target_pct*100, 2)) + '% · SL -' + str(round(pe.stoploss_pct*100, 2)) + '% (Settings → Target &amp; Stop Loss) · ' + mode_window_txt + ' · '
        'Score≥35 · Vote≥50% · Vol≥1.3×'
        '</div></div>'
        '<div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px;width:100%">'
        '<span class="b bg-gold"><i class="fas fa-thumbtack"></i> ' + str(len(pe.data['pinned'])) + ' Pinned</span>'
        '<span class="b ' + ('bb' if smry['open_positions'] > 0 else 'bn') + '">' + str(smry['open_positions']) + ' Open</span>'
        '<span class="b bb">' + str(smry['win_trades']) + ' ✅</span>'
        '<span class="b bs">' + str(smry['loss_trades']) + ' ❌</span>'
        '<span class="b bn">Win Rate: ' + str(smry['win_rate']) + '%</span>'
        '</div></div>'
    )
    tabs = (
        '<div class="tabs2">'
        '<div class="tab2 active" data-tab="overview" onclick="ptSwitchTab(\'overview\')">Overview</div>'
        '<div class="tab2" data-tab="positions" onclick="ptSwitchTab(\'positions\')">Positions</div>'
        '<div class="tab2" data-tab="orders" onclick="ptSwitchTab(\'orders\')">Orders</div>'
        '<div class="tab2" data-tab="trades" onclick="ptSwitchTab(\'trades\')">Trades</div>'
        '<div class="tab2" data-tab="daily" onclick="ptSwitchTab(\'daily\')">Daily</div>'
        '<div class="tab2" data-tab="pinned" onclick="ptSwitchTab(\'pinned\')">📋 Monitored</div>'
        '<div class="tab2" data-tab="siglog" onclick="ptSwitchTab(\'siglog\')">📊 Signal Log</div>'
        '<div class="tab2" data-tab="backtest" onclick="ptSwitchTab(\'backtest\')">📈 Backtest</div>'
        '<div class="tab2" data-tab="settings" onclick="ptSwitchTab(\'settings\')">⚙️ Settings</div>'
        '</div>'
        '<div id="ptTabContent"><div class="es"><div class="spin"></div><p style="margin-top:10px">Loading...</p></div></div>'
    )
    mkt_clock = '<div class="mkt-clock" id="mktClockWrap"></div>'
    return mkt_clock + banner + wallet_box + '<div id="ptCards"></div>' + tabs

# ==================== HTML TEMPLATE ====================
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>AlphaScanner Pro — Paper Trading</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=DM+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
:root{
--bg0:#0d1117;--bg1:#161b22;--bg2:#21262d;--bg3:#30363d;
--border:#30363d;--text0:#e6edf3;--text1:#c9d1d9;--text2:#8b949e;--text3:#6e7681;
--green:#3fb950;--green-b:#00e676;--red:#f85149;--red-b:#ff1744;
--blue:#58a6ff;--orange:#e3b341;--accent:#1f6feb;--accent2:#388bfd;--gold:#f0c040;
--sidebar-w:200px;--topbar-h:50px;
}
*{margin:0;padding:0;box-sizing:border-box}
html,body{background:var(--bg0);color:var(--text0);font-family:'DM Sans',sans-serif;min-height:100vh;font-size:13px;overflow-x:hidden}
::-webkit-scrollbar{width:5px;height:5px}::-webkit-scrollbar-track{background:var(--bg1)}::-webkit-scrollbar-thumb{background:var(--bg3);border-radius:3px}
.topbar{height:var(--topbar-h);background:var(--bg1);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 14px;gap:10px;position:sticky;top:0;z-index:200}
.tl{display:flex;align-items:center;gap:7px;font-family:'Space Mono',monospace;font-weight:700;font-size:13px;white-space:nowrap;flex-shrink:0}
.tl-dot{width:8px;height:8px;border-radius:50%;background:var(--green-b);animation:pulse 2s infinite;flex-shrink:0}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.hamburger{display:none;flex-direction:column;gap:4px;cursor:pointer;padding:6px;border-radius:5px;background:var(--bg2);border:1px solid var(--border);flex-shrink:0}
.hamburger span{width:18px;height:2px;background:var(--text1);border-radius:2px;transition:all .2s}
.tc-time{margin-left:auto;font-family:'Space Mono',monospace;font-size:10px;color:var(--text3);white-space:nowrap;flex-shrink:0}
.shell{display:flex;height:calc(100vh - var(--topbar-h));position:relative}
.sidebar{width:var(--sidebar-w);background:var(--bg1);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;flex-shrink:0;transition:transform .25s ease}
.sidebar-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:150}
.main{flex:1;overflow-y:auto;padding:12px;min-width:0}
.ns{padding:10px 8px 3px;font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;font-weight:700;font-family:'Space Mono',monospace}
.ni{display:flex;align-items:center;gap:8px;padding:7px 13px;cursor:pointer;color:var(--text2);font-size:12px;font-weight:500;border-left:2px solid transparent;transition:all .12s;margin:1px 0}
.ni:hover{background:var(--bg2);color:var(--text0)}
.ni.active{background:rgba(240,192,64,.12);color:var(--gold);border-left-color:var(--gold)}
.ni i{width:13px;font-size:11px;opacity:.8}
.mkt-panel{margin:auto 0 0;padding:10px;border-top:1px solid var(--border)}
.mkt-card{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:9px}
.mkt-row{display:flex;justify-content:space-between;padding:3px 0;border-bottom:1px solid rgba(48,54,61,.5)}
.mkt-row:last-child{border-bottom:none}
.mn{font-size:9px;color:var(--text3);font-family:'Space Mono',monospace}
.mv{font-size:11px;font-weight:700;font-family:'Space Mono',monospace}
.btn{display:inline-flex;align-items:center;gap:5px;padding:6px 12px;border:none;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;transition:all .12s;white-space:nowrap;font-family:'DM Sans',sans-serif}
.btn-p{background:var(--accent);color:white}.btn-p:hover:not(:disabled){background:var(--accent2)}
.btn-g{background:rgba(63,185,80,.12);color:var(--green);border:1px solid rgba(63,185,80,.25)}.btn-g:hover:not(:disabled){background:rgba(63,185,80,.2)}
.btn-r{background:rgba(248,81,73,.1);color:var(--red);border:1px solid rgba(248,81,73,.2)}.btn-r:hover:not(:disabled){background:rgba(248,81,73,.18)}
.btn-gh{background:var(--bg2);color:var(--text1);border:1px solid var(--border)}.btn-gh:hover:not(:disabled){background:var(--bg3)}
.btn-gold{background:rgba(240,192,64,.1);color:var(--gold);border:1px solid rgba(240,192,64,.3)}.btn-gold:hover:not(:disabled){background:rgba(240,192,64,.18)}
.btn-orange{background:rgba(227,179,65,.12);color:var(--orange);border:1px solid rgba(227,179,65,.3)}.btn-orange:hover:not(:disabled){background:rgba(227,179,65,.22)}
.btn:disabled{opacity:.3;cursor:not-allowed}
select,input[type=text],input[type=number]{background:var(--bg2);color:var(--text0);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:12px;font-family:'DM Sans',sans-serif;outline:none;max-width:100%}
select:focus,input:focus{border-color:var(--blue)}
.tw{overflow-x:auto;border:1px solid var(--border);border-radius:9px;-webkit-overflow-scrolling:touch}
table{width:100%;border-collapse:collapse;font-size:12px}
thead{background:var(--bg1)}
th{padding:7px 9px;text-align:left;font-size:9px;text-transform:uppercase;letter-spacing:.4px;color:var(--text3);font-weight:700;font-family:'Space Mono',monospace;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:7px 9px;border-bottom:1px solid rgba(48,54,61,.5);vertical-align:middle}
tr:last-child td{border-bottom:none}tr:hover td{background:rgba(255,255,255,.018)}
.section-header{display:flex;justify-content:space-between;align-items:center;padding:6px 0;margin:8px 0 5px;border-bottom:1px solid var(--border);flex-wrap:wrap;gap:4px}
.section-title{font-family:'Space Mono',monospace;font-size:10px;font-weight:700;color:var(--text3);text-transform:uppercase;letter-spacing:.5px}
.pin-count{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:var(--gold);color:#000;font-size:9px;font-weight:700;margin-left:4px}
.charges-box{background:rgba(248,81,73,.05);border:1px solid rgba(248,81,73,.15);border-radius:7px;padding:8px 12px;font-size:10px;font-family:'Space Mono',monospace;margin-bottom:10px}
.charges-box-title{color:var(--red);font-weight:700;margin-bottom:4px}
.charges-row{display:flex;justify-content:space-between;padding:2px 0;color:var(--text3)}
.charges-row.net-pos{color:var(--green);font-weight:700;border-top:1px solid rgba(63,185,80,.2);margin-top:4px;padding-top:4px}
.charges-row.net-neg{color:var(--red);font-weight:700;border-top:1px solid rgba(248,81,73,.2);margin-top:4px;padding-top:4px}
.wishlist-search{background:var(--bg1);border:1px solid var(--border);border-radius:9px;padding:12px;margin-bottom:12px}
.wishlist-search-title{font-family:'Space Mono',monospace;font-size:11px;font-weight:700;color:var(--gold);margin-bottom:8px}
.wishlist-search-input{position:relative;flex:1;min-width:200px}
.wishlist-search-field{width:100%;padding:8px 11px 8px 32px;background:var(--bg2);border:1px solid var(--border);border-radius:7px;color:var(--text0);font-size:12px;font-family:'Space Mono',monospace;outline:none}
.wishlist-search-field:focus{border-color:var(--gold)}
.wishlist-search-icon{position:absolute;left:10px;top:50%;transform:translateY(-50%);color:var(--text3);font-size:12px}
.wishlist-dropdown{position:absolute;top:100%;left:0;right:0;background:var(--bg2);border:1px solid var(--border);border-radius:7px;max-height:250px;overflow-y:auto;z-index:200;display:none}
.wishlist-item{padding:8px 12px;cursor:pointer;font-size:11px;color:var(--text1);font-family:'Space Mono',monospace;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.wishlist-item:last-child{border-bottom:none}.wishlist-item:hover{background:var(--bg3);color:var(--text0)}
.wishlist-item-add{color:var(--gold);font-size:10px}
.toast{position:fixed;bottom:20px;right:16px;background:#21262d;border:1px solid #30363d;color:#e6edf3;padding:9px 14px;border-radius:8px;font-size:12px;font-family:'Space Mono',monospace;z-index:9999;box-shadow:0 4px 20px rgba(0,0,0,.4);animation:toastIn .2s ease;max-width:calc(100vw - 32px)}
@keyframes toastIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.sg{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px;margin-bottom:12px}
.sc2{background:var(--bg1);border:1px solid var(--border);border-radius:9px;padding:10px 12px;transition:border-color .15s}
.sc2:hover{border-color:var(--accent)}.sc2 .v{font-size:18px;font-weight:700;font-family:'Space Mono',monospace}
.sc2 .l{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.4px;margin-top:2px}
.tabs2{display:flex;gap:2px;margin-bottom:10px;background:var(--bg1);border:1px solid var(--border);border-radius:8px;padding:3px;overflow-x:auto;scrollbar-width:none}
.tabs2::-webkit-scrollbar{display:none}
.tab2{flex:1;text-align:center;padding:5px 8px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600;font-family:'Space Mono',monospace;color:var(--text3);transition:all .12s;white-space:nowrap;min-width:60px}
.tab2:hover{color:var(--text1);background:var(--bg2)}.tab2.active{background:var(--accent);color:white}
.pin-btn{display:inline-flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:5px;border:1px solid var(--border);background:var(--bg2);cursor:pointer;color:var(--text3);font-size:11px;transition:all .15s}
.pin-btn:hover{background:rgba(240,192,64,.15);color:var(--gold);border-color:rgba(240,192,64,.4)}
.pin-btn.pinned{background:rgba(240,192,64,.12);color:var(--gold);border-color:rgba(240,192,64,.4)}
.pt-banner{background:linear-gradient(135deg,rgba(240,192,64,.08),rgba(31,111,235,.06));border:1px solid rgba(240,192,64,.2);border-radius:10px;padding:12px 14px;margin-bottom:12px;display:flex;align-items:flex-start;gap:10px;flex-wrap:wrap}
.mkt-clock{display:flex;align-items:center;gap:10px;background:rgba(0,0,0,.25);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:10px 14px;margin-bottom:10px;font-family:'Space Mono',monospace;flex-wrap:wrap}
.mkt-clock-bar{flex:1;min-width:120px;height:6px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden}
.mkt-clock-bar-fill{height:100%;border-radius:3px;transition:width 1s linear}
.mkt-status-pill{font-size:10px;font-weight:700;padding:3px 10px;border-radius:20px;letter-spacing:1px;text-transform:uppercase}
.mkt-ist{font-size:11px;color:var(--text3);margin-left:auto}
.wallet-box{background:var(--bg1);border:1px solid rgba(240,192,64,.25);border-radius:9px;padding:10px 14px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.wallet-amt{font-family:'Space Mono',monospace;font-size:18px;font-weight:700;color:var(--gold)}
.wallet-avail{font-family:'Space Mono',monospace;font-size:11px;color:var(--text3)}
.pos-card{background:var(--bg1);border:1px solid var(--border);border-radius:9px;padding:10px 12px;display:flex;align-items:center;gap:10px;margin-bottom:8px;transition:border-color .15s;flex-wrap:wrap}
.pos-card:hover{border-color:var(--accent)}
.pos-sym{font-family:'Space Mono',monospace;font-weight:700;font-size:13px;min-width:80px}
.pos-side{padding:2px 8px;border-radius:3px;font-size:9px;font-weight:700;font-family:'Space Mono',monospace}
.pos-buy{background:rgba(63,185,80,.12);color:var(--green);border:1px solid rgba(63,185,80,.2)}
.pos-sell{background:rgba(248,81,73,.1);color:var(--red);border:1px solid rgba(248,81,73,.2)}
.pos-pnl{font-family:'Space Mono',monospace;font-size:13px;font-weight:700}
.pnl-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:20px;font-family:'Space Mono',monospace;font-size:11px;font-weight:700}
.pnl-pos{background:rgba(63,185,80,.1);color:var(--green);border:1px solid rgba(63,185,80,.2)}
.pnl-neg{background:rgba(248,81,73,.08);color:var(--red);border:1px solid rgba(248,81,73,.15)}
.pnl-zero{background:rgba(139,148,158,.08);color:var(--text2);border:1px solid rgba(139,148,158,.15)}
.b{display:inline-block;padding:2px 6px;border-radius:3px;font-size:9px;font-weight:700;letter-spacing:.2px;font-family:'Space Mono',monospace;margin:1px}
.bb{background:rgba(63,185,80,.12);color:var(--green);border:1px solid rgba(63,185,80,.2)}
.bs{background:rgba(248,81,73,.1);color:var(--red);border:1px solid rgba(248,81,73,.2)}
.bn{background:rgba(139,148,158,.08);color:var(--text2);border:1px solid rgba(139,148,158,.15)}
.bg-gold{background:rgba(240,192,64,.1);color:var(--gold);border:1px solid rgba(240,192,64,.3)}
.sym{font-family:'Space Mono',monospace;font-weight:700;font-size:12px}
.num{font-family:'Space Mono',monospace;font-size:11px;color:var(--text2)}
.pos{color:var(--green);font-family:'Space Mono',monospace;font-weight:700}
.neg{color:var(--red);font-family:'Space Mono',monospace;font-weight:700}
.es{display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:180px;color:var(--text3)}
.es i{font-size:30px;margin-bottom:9px;opacity:.2}.es p{font-size:12px;text-align:center;padding:0 20px}
.spin{width:18px;height:18px;border:2px solid var(--bg3);border-top-color:var(--blue);border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
.scan-progress-bar{height:6px;background:rgba(255,255,255,.07);border-radius:3px;overflow:hidden;margin:8px 0}
.scan-progress-fill{height:100%;border-radius:3px;background:linear-gradient(90deg,var(--blue),var(--gold));transition:width .4s}
.scan-config-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-bottom:10px}
.rgrid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.rgrid4{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;align-items:end}
@media(max-width:767px){
.hamburger{display:flex}
.sidebar{position:fixed;left:0;top:var(--topbar-h);bottom:0;z-index:160;width:240px;transform:translateX(-100%);box-shadow:4px 0 20px rgba(0,0,0,.5)}
.sidebar.open{transform:translateX(0)}
.sidebar-overlay{display:block;opacity:0;pointer-events:none;transition:opacity .25s}
.sidebar-overlay.visible{opacity:1;pointer-events:all}
.main{padding:8px}
.sg{grid-template-columns:repeat(2,1fr)}
.tabs2{white-space:nowrap;flex-wrap:nowrap}
.tab2{flex:0 0 auto;padding:6px 14px}
}
@media(max-width:480px){
    .rgrid2, .rgrid4, .scan-config-grid, .rgrid4 > * { grid-template-columns:1fr !important; }
}
</style>
</head>
<body>
<div class="sidebar-overlay" id="sidebarOverlay" onclick="closeSidebar()"></div>
<div class="topbar">
<button class="hamburger" id="hamburgerBtn" onclick="toggleSidebar()" aria-label="Menu"><span></span><span></span><span></span></button>
<div class="tl"><div class="tl-dot"></div><span>ALPHA SCANNER PRO</span></div>
<div style="display:flex;gap:4px;margin-left:8px;flex-shrink:0">
    <div style="padding:3px 8px;border-radius:5px;background:var(--bg2);border:1px solid var(--border);font-size:11px;font-family:'Space Mono',monospace">
    <span style="color:var(--text3);font-size:9px">PINNED</span> <span style="color:var(--gold)" id="topPinnedCount">{{ pinned_count }}</span>
    </div>
</div>
<div class="tc-time" id="clk"></div>
</div>
<div class="shell">
<nav class="sidebar" id="sidebar">
<div class="ns">Algo Trading</div>
<div class="ni active">
    <i class="fas fa-robot"></i> Paper Trading
    <span class="pin-count" id="sidebarPinCount">{{ pinned_count }}</span>
</div>
<div class="mkt-panel">
    <div class="mkt-card">
    <div class="mkt-row"><span class="mn">NIFTY</span><span class="mv" id="idx_NIFTY" style="color:var(--text3)">—</span></div>
    <div class="mkt-row"><span class="mn">BANKNIFTY</span><span class="mv" id="idx_BANKNIFTY" style="color:var(--text3)">—</span></div>
    <div class="mkt-row"><span class="mn">VIX</span><span class="mv" id="idx_VIX" style="color:var(--text3)">—</span></div>
    <div class="mkt-row"><span class="mn">SENSEX</span><span class="mv" id="idx_SENSEX" style="color:var(--text3)">—</span></div>
    </div>
</div>
</nav>
<main class="main">
<div id="content">{{ content|safe }}</div>
</main>
</div>
<script>
var PINNED_SYMS={{ pinned_symbols|tojson }};
var ALL_SYMS={{ all_symbols|tojson }};
var AVAILABLE_STRATEGIES = {{ available_strategies|tojson }};
var CURRENT_STRATEGY = {{ current_strategy|tojson }};
var CURRENT_MODE = {{ current_mode|tojson }};
var CURRENT_RISK = {{ current_risk|tojson }};
var _backtestWallet = 100000;
var _backtestFromDate = '';
var _backtestToDate = '';
var _backtestMode = CURRENT_MODE || 'INTRADAY';
var _backtestTargetPct = null;
var _backtestSlPct = null;
var _backtestMinHoldDays = (typeof CURRENT_RISK !== 'undefined' && CURRENT_RISK.min_hold_days_delivery != null) ? CURRENT_RISK.min_hold_days_delivery : 1;
var _backtestRunning = false;
var _backtestResults = null;
var _backtestPollTimer = null;
var _lastOrderCount=-1;
var _prevIndices={};
var ptTab='overview', ptData={};
var _sigLogAllLogs=[],_sigLogPage=1,_sigLogPP=25,_sigLogFilter='all',_sigLogFetching=false;
var ptRefreshTimer=null;
var _pinnableSuggestionsLoaded=false;
var _scanPollingTimer=null;
const PT_REFRESH_INTERVAL=5000;
const PT_BG_REFRESH_INTERVAL=30000;

function tick(){var d=new Date();var el=document.getElementById('clk');if(el)el.textContent=d.toLocaleDateString('en-IN')+' '+d.toTimeString().slice(0,8);}
setInterval(tick,1000);tick();

function fetchIndices(){
fetch('/market-indices').then(r=>r.json()).then(d=>{
    if(!d.indices)return;
    d.indices.forEach(idx=>{
    var el=document.getElementById('idx_'+idx.label);
    if(!el)return;
    if(idx.ltp==null){el.textContent='—';el.style.color='var(--text3)';return;}
    var prev=_prevIndices[idx.label];var val=idx.ltp;
    var fmt=idx.kind==='vix'?val.toFixed(2):val.toLocaleString('en-IN',{maximumFractionDigits:2});
    el.textContent=fmt;
    if(prev!=null) el.style.color=val>prev?'var(--green-b)':val<prev?'var(--red-b)':idx.kind==='vix'?'var(--orange)':'var(--green)';
    else el.style.color=idx.kind==='vix'?'var(--orange)':'var(--green)';
    _prevIndices[idx.label]=val;
    });
}).catch(()=>{});
}
fetchIndices();setInterval(fetchIndices,10000);

function toggleSidebar(){var sb=document.getElementById('sidebar');var ov=document.getElementById('sidebarOverlay');var open=sb.classList.toggle('open');ov.classList.toggle('visible',open);document.body.style.overflow=open?'hidden':'';}
function closeSidebar(){document.getElementById('sidebar').classList.remove('open');document.getElementById('sidebarOverlay').classList.remove('visible');document.body.style.overflow='';}
function showToast(msg){var t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.appendChild(t);setTimeout(()=>t.remove(),2500);}

function isPinned(sym){return PINNED_SYMS.indexOf(sym)>=0;}

function initPT(){
loadPTData();
startPTRefresh(PT_REFRESH_INTERVAL);
startMarketClock();
initWishlistSearch();
}
function startPTRefresh(iv){if(ptRefreshTimer)clearInterval(ptRefreshTimer);ptRefreshTimer=setInterval(loadPTData,iv);}
function stopPTRefresh(){if(ptRefreshTimer){clearInterval(ptRefreshTimer);ptRefreshTimer=null;}}

function showOrderAlert(title,body,color){
var ex=document.getElementById('orderAlertBanner');if(ex)ex.remove();
var el=document.createElement('div');el.id='orderAlertBanner';
el.style.cssText='position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:9999;background:#161b22;border:2px solid '+color+';border-radius:12px;padding:16px 22px 14px;box-shadow:0 8px 40px rgba(0,0,0,.7);min-width:290px;max-width:92vw;animation:toastIn .3s ease;font-family:Space Mono,monospace;cursor:default;';
el.innerHTML='<div style="color:'+color+';font-weight:700;font-size:13px;margin-bottom:5px;padding-right:22px">'+title+'</div><div style="color:#c9d1d9;font-size:11px;line-height:1.6">'+body+'</div><button onclick="this.parentElement.remove()" style="position:absolute;top:9px;right:11px;background:none;border:none;color:#8b949e;cursor:pointer;font-size:15px;line-height:1">✕</button>';
document.body.appendChild(el);setTimeout(()=>{if(el.parentElement)el.remove();},9000);
}

function startMarketClock(){updateMarketClock();setInterval(updateMarketClock,1000);}
function updateMarketClock(){
var el=document.getElementById('mktClockWrap');if(!el)return;
var now=new Date();var utc=now.getTime()+now.getTimezoneOffset()*60000;var ist=new Date(utc+5.5*3600000);
var h=ist.getHours(),m=ist.getMinutes(),s=ist.getSeconds();
var dow=ist.getDay();var isWday=dow>=1&&dow<=5;var tot=h*60+m;
var OPEN=9*60+15,CLOSE=15*60+30,SQOFF=15*60+15;
var istStr=('0'+h).slice(-2)+':'+('0'+m).slice(-2)+':'+('0'+s).slice(-2)+' IST';
function pad2(n){return ('0'+n).slice(-2);}
function fmtC(secs){var hh=Math.floor(secs/3600),mm=Math.floor((secs%3600)/60),ss=secs%60;return (hh?pad2(hh)+':':'')+pad2(mm)+':'+pad2(ss);}
var tHtml='',lHtml='',bHtml='',pHtml='',nHtml='';
if(!isWday){
    var dtm=(8-dow)%7||7;var stm=dtm*86400-(h*3600+m*60+s)+OPEN*60;
    pHtml='<span class="mkt-status-pill" style="background:rgba(139,148,158,.15);color:#8b949e">WEEKEND</span>';
    lHtml='Opens Monday in';tHtml='<span style="font-size:22px;font-weight:700;letter-spacing:2px;color:#8b949e">'+fmtC(stm)+'</span>';nHtml='9:15 AM IST Monday';
}else if(tot<OPEN){
    var sl=(OPEN-tot)*60-s;
    pHtml='<span class="mkt-status-pill" style="background:rgba(240,192,64,.15);color:var(--gold)">PRE-MARKET</span>';
    lHtml='⏳ Opens in';tHtml='<span style="font-size:22px;font-weight:700;letter-spacing:2px;color:var(--gold)">'+fmtC(sl)+'</span>';nHtml='Opens 9:15 AM';
}else if(tot>=OPEN&&tot<SQOFF){
    var sl2=(SQOFF-tot)*60-s;var ts2=(SQOFF-OPEN)*60;var prog=Math.min(100,(ts2-sl2)/ts2*100);
    pHtml='<span class="mkt-status-pill" style="background:rgba(0,230,118,.15);color:var(--green)">● LIVE</span>';
    lHtml='⏱ SqOff in';tHtml='<span style="font-size:22px;font-weight:700;letter-spacing:2px;color:var(--green)">'+fmtC(sl2)+'</span>';
    bHtml='<div class="mkt-clock-bar"><div class="mkt-clock-bar-fill" style="width:'+prog.toFixed(2)+'%;background:linear-gradient(90deg,var(--green),var(--gold))"></div></div>';
    nHtml='SqOff 3:15 · Close 3:30';
}else if(tot>=SQOFF&&tot<CLOSE){
    var sl3=(CLOSE-tot)*60-s;
    pHtml='<span class="mkt-status-pill" style="background:rgba(255,23,68,.15);color:var(--red)">SQ-OFF</span>';
    lHtml='⚠️ Closes in';tHtml='<span style="font-size:22px;font-weight:700;letter-spacing:2px;color:var(--orange)">'+fmtC(sl3)+'</span>';nHtml='Squaring off';
}else{
    var stn=(OPEN+24*60-tot)*60-s;if(dow===5)stn+=2*86400;
    pHtml='<span class="mkt-status-pill" style="background:rgba(139,148,158,.12);color:#8b949e">CLOSED</span>';
    lHtml='Next session in';tHtml='<span style="font-size:22px;font-weight:700;letter-spacing:2px;color:#8b949e">'+fmtC(stn)+'</span>';nHtml='Next: '+(dow===5?'Monday':'Tomorrow')+' 9:15 AM';
}
el.innerHTML='<div style="display:flex;flex-direction:column;gap:2px;flex:1;min-width:0">'
    +'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'+pHtml
    +'<span style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:1px">'+lHtml+'</span></div>'
    +tHtml+(bHtml?'<div style="margin-top:5px">'+bHtml+'</div>':'')
    +'<div style="font-size:9px;color:var(--text3);margin-top:2px">'+nHtml+'</div>'
    +_slotBadge(tot,isWday)+'</div>'
    +'<span class="mkt-ist">'+istStr+'</span>';}

function _slotBadge(tot,isWday){
if(!isWday)return '';
var S1s=9*60+15,S1e=14*60+30;
var inS1=(tot>=S1s&&tot<=S1e);
var c,lbl;
if(inS1){c='#00e676';lbl='🟢 TRADING WINDOW ACTIVE — 9:15–14:30 ⭐⭐⭐⭐⭐';}
else{c='#8b949e';lbl='⏸ Outside Trading Window';}
return '<div style="margin-top:6px;font-size:10px;font-family:Space Mono,monospace;color:'+c+';background:rgba(0,0,0,.25);border:1px solid '+c+'44;border-radius:5px;padding:3px 8px;display:inline-block">'+lbl+'</div>';
}

function loadPTData(){
    fetch('/paper/summary').then(r=>r.json()).then(d=>{
        if(_lastOrderCount>=0&&(d.order_count||0)>_lastOrderCount&&d.latest_order){
            var o=d.latest_order;var isBuy=o.side==='BUY';var clr=isBuy?'#00e676':'#ff1744';
            showOrderAlert((isBuy?'🟢 BUY':'🔴 SELL')+' ORDER — '+o.symbol,
                'Qty: '+o.qty+'  ·  Price: ₹'+Number(o.price).toFixed(2)+'  ·  Value: ₹'+Number(o.value).toFixed(2)
                +(o.total_charges?' · Charges: ₹'+Number(o.total_charges).toFixed(2):'')
                +'  ·  Score: '+(o.signal_score||'—')+'<br><span style="color:#8b949e">'+o.time+'</span>',clr);
        }
        _lastOrderCount=d.order_count||0;
        ptData=d;
        renderPTSummaryCards(d);
        // Only re-render the tab content if it is NOT the Settings or
        // Backtest tab. Both hold live user-editable form inputs — Backtest
        // in particular has a native <input type="date">, whose calendar
        // popup gets silently closed if the whole tab's innerHTML is
        // replaced out from under it mid-interaction by this 5s refresh.
        if (ptTab !== 'settings' && ptTab !== 'backtest') {
            renderPTTab(ptTab);
        }
        // If settings/backtest tab is active, do NOT re-render it – keep user input (and any open date picker) intact
    }).catch(err=>console.error('[PT]',err));
}

function renderPTSummaryCards(d){
var tot=d.total_pnl||0,real=d.realized_pnl||0,unreal=d.unrealized_pnl||0;
var chg=d.total_charges_paid||0,wallet=d.wallet||0,avail=d.available||0;
function pnlCls(v){return v>0?'style="color:var(--green)"':v<0?'style="color:var(--red)"':'style="color:var(--text2)"';}
function fp(v){return (v>0?'+':'')+'₹'+Math.abs(v).toFixed(2);}
function fi(v){return '₹'+Number(v).toLocaleString('en-IN',{maximumFractionDigits:0});}
var wbW=document.getElementById('wbWallet'),wbA=document.getElementById('wbAvail'),wbT=document.getElementById('wbTotalPnl'),wbI=document.getElementById('walletInput');
if(wbW)wbW.textContent=fi(wallet);
if(wbA)wbA.innerHTML='Available: '+fi(avail)+'  ·  Net Realized: <span style="color:'+(real>=0?'var(--green-b)':'var(--red-b)')+'">'+fp(real)+'</span>  ·  <span style="color:var(--red);font-size:10px">Charges: ₹'+chg.toFixed(2)+'</span>';
if(wbT){wbT.textContent=fp(tot);wbT.style.color=tot>=0?'var(--green-b)':'var(--red-b)';}
if(wbI&&document.activeElement!==wbI)wbI.value=Math.round(wallet);
var html='<div class="sg">'
    +'<div class="sc2" style="border-color:rgba(240,192,64,.3)"><div class="v" style="color:var(--gold)">'+fi(wallet)+'</div><div class="l">Wallet</div></div>'
    +'<div class="sc2"><div class="v" style="color:var(--blue)">'+fi(avail)+'</div><div class="l">Available</div></div>'
    +'<div class="sc2"><div class="v" '+pnlCls(tot)+'>'+fp(tot)+'</div><div class="l">Total P&L</div></div>'
    +'<div class="sc2"><div class="v" '+pnlCls(real)+'>'+fp(real)+'</div><div class="l">Realized</div></div>'
    +'<div class="sc2"><div class="v" style="color:var(--red)">₹'+chg.toFixed(2)+'</div><div class="l">Charges</div></div>'
    +'<div class="sc2"><div class="v" '+pnlCls(unreal)+'>'+fp(unreal)+'</div><div class="l">Unrealized</div></div>'
    +'<div class="sc2"><div class="v">'+(d.win_rate||0)+'%</div><div class="l">Win Rate</div></div>'
    +'<div class="sc2"><div class="v" style="color:var(--gold)">'+(d.pinned_count||0)+'</div><div class="l">Monitored</div></div>'
    +'</div>';
var el=document.getElementById('ptCards');if(el)el.innerHTML=html;
}

function ptSwitchTab(tab){
if(ptTab==='pinned'&&tab!=='pinned')_pinnableSuggestionsLoaded=false;
if(ptTab==='siglog'&&tab!=='siglog')stopSigLogPolling();
ptTab=tab;
document.querySelectorAll('.tab2').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab));
renderPTTab(tab);
if(tab==='siglog')startSigLogPolling();
}
function renderPTTab(tab){
var el=document.getElementById('ptTabContent');if(!el)return;
if(tab==='overview')renderPTOverview(el);
else if(tab==='positions')renderPTPositions(el);
else if(tab==='orders')renderPTOrders(el);
else if(tab==='trades')renderPTTrades(el);
else if(tab==='daily')renderPTDaily(el);
else if(tab==='pinned')renderPTPinned(el);
else if(tab==='siglog')renderPTSigLog(el);
else if(tab==='backtest')renderPTBacktest(el);
else if(tab==='settings')renderPTSettings(el);
}

// ─── SETTINGS TAB ─────────────────────────────────────────
function saveRiskConfig(){
    var targetIntraday = document.getElementById('targetIntraday').value;
    var slIntraday = document.getElementById('slIntraday').value;
    var targetDelivery = document.getElementById('targetDelivery').value;
    var slDelivery = document.getElementById('slDelivery').value;
    var minHoldDelivery = document.getElementById('minHoldDelivery').value;
    var status = document.getElementById('riskStatus');
    status.innerHTML='<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Saving...</span>';
    var payload = {
        target_pct_intraday: targetIntraday,
        stoploss_pct_intraday: slIntraday,
        target_pct_delivery: targetDelivery,
        stoploss_pct_delivery: slDelivery,
        min_hold_days_delivery: minHoldDelivery
    };
    fetch('/api/user/update-risk', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
    })
    .then(r=>r.json())
    .then(d=>{
        if(d.status==='ok'){
            status.innerHTML='<span style="color:var(--green);">✅ '+d.msg+'</span>';
            showToast('✅ Target/Stop-Loss updated');
            // Update CURRENT_RISK with new values
            CURRENT_RISK = d.risk_config;
            // Reload the page to reflect changes everywhere
            setTimeout(()=>location.reload(), 1000);
        } else {
            status.innerHTML='<span style="color:var(--red);">❌ '+d.msg+'</span>';
        }
    })
    .catch(e=>{
        status.innerHTML='<span style="color:var(--red);">❌ Network error: '+e.message+'</span>';
    });
}

function renderPTSettings(el){
    el.innerHTML=`
        <div style="padding:0;">
            <h2 style="color:var(--gold);font-family:Space Mono,monospace;font-size:18px;margin-bottom:12px;">⚙️ Settings</h2>
            <div class="rgrid2">
                <div style="background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;">
                    <h3 style="color:var(--text1);font-size:14px;margin-bottom:12px;">📊 Strategy</h3>
                    <select id="strategySelect" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;margin-bottom:12px;">
                        ${AVAILABLE_STRATEGIES.map(name => `<option value="${name}" ${name===CURRENT_STRATEGY?'selected':''}>${name}</option>`).join('')}
                    </select>
                    <button class="btn btn-gold" onclick="saveStrategy()" style="width:100%;justify-content:center;padding:8px;"><i class="fas fa-save"></i> Save Strategy</button>
                    <div id="strategyStatus" style="margin-top:8px;font-size:12px;color:var(--text2);text-align:center;"></div>
                </div>
                <div style="background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;">
                    <h3 style="color:var(--text1);font-size:14px;margin-bottom:12px;">💼 Trading Mode</h3>
                    <p style="font-size:10px;color:var(--text3);margin-bottom:10px;line-height:1.5;">
                        <b>Intraday (MIS):</b> ~5x leverage, auto square-off at 15:15, restricted to trade slots.<br>
                        <b>Delivery (CNC):</b> full cash per share, no leverage, no forced square-off.
                    </p>
                    <select id="modeSelect" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;margin-bottom:12px;">
                        <option value="INTRADAY" ${CURRENT_MODE==='INTRADAY'?'selected':''}>Intraday (MIS · Leveraged)</option>
                        <option value="DELIVERY" ${CURRENT_MODE==='DELIVERY'?'selected':''}>Delivery (CNC · Cash)</option>
                    </select>
                    <button class="btn btn-gold" onclick="saveMode()" style="width:100%;justify-content:center;padding:8px;"><i class="fas fa-save"></i> Save Trading Mode</button>
                    <div id="modeStatus" style="margin-top:8px;font-size:12px;color:var(--text2);text-align:center;"></div>
                </div>
                <div style="background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;">
                    <h3 style="color:var(--text1);font-size:14px;margin-bottom:12px;">🎯 Target &amp; Stop Loss</h3>
                    <p style="font-size:10px;color:var(--text3);margin-bottom:10px;line-height:1.5;">
                        Used by live paper trading <b>and</b> Backtest. Set separately per mode — Intraday trades tight and fast, Delivery/CNC usually wants more room.
                    </p>
                    <div class="rgrid2" style="gap:10px;">
                        <div>
                            <label style="display:block;font-size:10px;color:var(--gold);margin-bottom:4px;">Intraday Target %</label>
                            <input type="number" id="targetIntraday" value="${CURRENT_RISK.target_pct_intraday}" step="0.1" min="0.1" max="20" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                        </div>
                        <div>
                            <label style="display:block;font-size:10px;color:var(--red);margin-bottom:4px;">Intraday Stop Loss %</label>
                            <input type="number" id="slIntraday" value="${CURRENT_RISK.stoploss_pct_intraday}" step="0.1" min="0.1" max="20" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                        </div>
                        <div>
                            <label style="display:block;font-size:10px;color:var(--gold);margin-bottom:4px;">Delivery/CNC Target %</label>
                            <input type="number" id="targetDelivery" value="${CURRENT_RISK.target_pct_delivery}" step="0.1" min="0.1" max="20" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                        </div>
                        <div>
                            <label style="display:block;font-size:10px;color:var(--red);margin-bottom:4px;">Delivery/CNC Stop Loss %</label>
                            <input type="number" id="slDelivery" value="${CURRENT_RISK.stoploss_pct_delivery}" step="0.1" min="0.1" max="20" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                        </div>
                        <div>
                            <label style="display:block;font-size:10px;color:var(--gold);margin-bottom:4px;">CNC Min Hold (days)</label>
                            <input type="number" id="minHoldDelivery" value="${CURRENT_RISK.min_hold_days_delivery}" step="1" min="0" max="30" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                        </div>
                    </div>
                    <p style="font-size:10px;color:var(--text3);margin-top:10px;line-height:1.5;">
                        <b>CNC Min Hold</b> also applies live — the monitor loop won't auto-exit a Delivery position on TARGET/STOP_LOSS until it's been held this many calendar days. The manual Exit button always works regardless.
                    </p>
                    <button class="btn btn-gold" onclick="saveRiskConfig()" style="width:100%;justify-content:center;padding:8px;margin-top:12px;"><i class="fas fa-save"></i> Save Target/Stop Loss</button>
                    <div id="riskStatus" style="margin-top:8px;font-size:12px;color:var(--text2);text-align:center;"></div>
                </div>
                <div style="background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;">
                    <h3 style="color:var(--text1);font-size:14px;margin-bottom:12px;">🔑 API Keys</h3>
                    <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">Kite API Key</label>
                    <input type="text" id="settingsApiKey" placeholder="Enter your API key" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;margin-bottom:12px;">
                    <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">Kite API Secret</label>
                    <input type="password" id="settingsApiSecret" placeholder="Enter your API secret" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;margin-bottom:12px;">
                    <button class="btn btn-gold" onclick="saveApiKeys()" style="width:100%;justify-content:center;padding:8px;"><i class="fas fa-save"></i> Save API Keys</button>
                    <div id="settingsStatus" style="margin-top:8px;font-size:12px;color:var(--text2);text-align:center;"></div>
                </div>
            </div>
            <div style="margin-top:16px;font-size:10px;color:var(--text3);text-align:center;border-top:1px solid var(--border);padding-top:12px;">
                <i class="fas fa-shield-alt" style="margin-right:5px;"></i> Keys are encrypted using AES‑256 (Fernet) before storage.
            </div>
        </div>
    `;
}

function saveMode(){
var sel = document.getElementById('modeSelect');
var mode = sel.value;
var status = document.getElementById('modeStatus');
status.innerHTML='<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Saving...</span>';
fetch('/api/user/update-mode', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({mode: mode})
})
.then(r=>r.json())
.then(d=>{
    if(d.status==='ok'){
        status.innerHTML='<span style="color:var(--green);">✅ '+d.msg+'</span>';
        showToast('✅ Trading mode set to '+mode);
        CURRENT_MODE = mode;
        setTimeout(()=>location.reload(), 1000);
    } else {
        status.innerHTML='<span style="color:var(--red);">❌ '+d.msg+'</span>';
    }
})
.catch(e=>{
    status.innerHTML='<span style="color:var(--red);">❌ Network error: '+e.message+'</span>';
});
}

function saveStrategy(){
var sel = document.getElementById('strategySelect');
var strategy = sel.value;
var status = document.getElementById('strategyStatus');
status.innerHTML='<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Saving...</span>';
fetch('/api/user/update-strategy', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({strategy: strategy})
})
.then(r=>r.json())
.then(d=>{
    if(d.status==='ok'){
        status.innerHTML='<span style="color:var(--green);">✅ '+d.msg+'</span>';
        showToast('✅ Strategy updated to '+strategy);
        setTimeout(()=>location.reload(), 1000);
    } else {
        status.innerHTML='<span style="color:var(--red);">❌ '+d.msg+'</span>';
    }
})
.catch(e=>{
    status.innerHTML='<span style="color:var(--red);">❌ Network error: '+e.message+'</span>';
});
}

function saveApiKeys(){
var key = document.getElementById('settingsApiKey').value.trim();
var secret = document.getElementById('settingsApiSecret').value.trim();
var status = document.getElementById('settingsStatus');
if(!key || !secret){ status.innerHTML='<span style="color:var(--red);">Both fields are required.</span>'; return; }
status.innerHTML='<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Saving...</span>';
fetch('/api/user/update-keys', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({kite_api_key: key, kite_api_secret: secret})
})
.then(r=>r.json())
.then(d=>{
    if(d.status==='ok'){
        status.innerHTML='<span style="color:var(--green);">✅ '+d.msg+'</span>';
        showToast('✅ API keys updated successfully!');
        document.getElementById('settingsApiKey').value='';
        document.getElementById('settingsApiSecret').value='';
    } else {
        status.innerHTML='<span style="color:var(--red);">❌ '+d.msg+'</span>';
    }
})
.catch(e=>{
    status.innerHTML='<span style="color:var(--red);">❌ Network error: '+e.message+'</span>';
});
}

// ─── BACKTEST TAB ─────────────────────────────────────────
function renderPTBacktest(el) {
    var wallet = _backtestWallet || 100000;
    var fromDate = _backtestFromDate || '';
    var toDate = _backtestToDate || '';
    var mode = _backtestMode || CURRENT_MODE || 'INTRADAY';
    var targetPct = _backtestTargetPct !== null ? _backtestTargetPct : '';
    var slPct = _backtestSlPct !== null ? _backtestSlPct : '';
    var minHoldDays = _backtestMinHoldDays != null ? _backtestMinHoldDays : 1;
    var running = _backtestRunning;
    var results = _backtestResults;
    
    var html = `
        <div style="padding:0;">
            <h2 style="color:var(--gold);font-family:Space Mono,monospace;font-size:18px;margin-bottom:12px;">📈 Backtest</h2>
            <p style="color:var(--text3);font-size:12px;margin-bottom:16px;">Test your selected strategy on pinned stocks over a custom date range.</p>
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;">
                <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(140px, 1fr));gap:12px;align-items:end;">
                    <div>
                        <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">Initial Wallet (₹)</label>
                        <input type="number" id="backtestWallet" value="${wallet}" step="10000" min="1000" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                    </div>
                    <div>
                        <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">From Date</label>
                        <input type="date" id="backtestFromDate" value="${fromDate}" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                    </div>
                    <div>
                        <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">To Date</label>
                        <input type="date" id="backtestToDate" value="${toDate}" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                    </div>
                    <div>
                        <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">Mode</label>
                        <select id="backtestMode" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                            <option value="INTRADAY" ${mode==='INTRADAY'?'selected':''}>Intraday (MIS)</option>
                            <option value="DELIVERY" ${mode==='DELIVERY'?'selected':''}>Delivery (CNC)</option>
                        </select>
                    </div>
                    <div>
                        <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">Target % (override)</label>
                        <input type="number" id="backtestTargetPct" value="${targetPct}" step="0.1" min="0.1" max="20" placeholder="Saved" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                    </div>
                    <div>
                        <label style="display:block;font-size:11px;color:var(--text3);margin-bottom:4px;">Stop Loss % (override)</label>
                        <input type="number" id="backtestSlPct" value="${slPct}" step="0.1" min="0.1" max="20" placeholder="Saved" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                    </div>
                    <div>
                        <label style="display:block;font-size:11px;color:var(--gold);margin-bottom:4px;">Min Hold Days (CNC only)</label>
                        <input type="number" id="backtestMinHoldDays" value="${minHoldDays}" step="1" min="0" max="30" style="width:100%;padding:8px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text0);font-family:Space Mono,monospace;font-size:12px;">
                    </div>
                </div>
                <p style="font-size:10px;color:var(--text3);margin-top:8px;line-height:1.5;">
                    <b>Min Hold Days</b> blocks TARGET/STOP_LOSS exits on a Delivery/CNC position until it has been held this many calendar days — mirrors real swing/investment behavior instead of same-session flips. Ignored in Intraday mode. Set to 0 to allow same-day exits.
                </p>
                <button class="btn btn-p" onclick="runBacktest()" id="backtestRunBtn" style="width:100%;justify-content:center;padding:10px;margin-top:12px;" ${running?'disabled':''}>
                    <i class="fas fa-play"></i> ${running?'Running...':'Run Backtest'}
                </button>
                <div id="backtestStatus" style="margin-top:10px;font-size:12px;color:var(--text2);text-align:center;">
                    ${running ? '<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Running backtest...</span>' : ''}
                    ${results && !running ? '<span style="color:var(--green);">✅ Backtest completed</span>' : ''}
                </div>
            </div>
            <div id="backtestResults" style="display:${results ? 'block' : 'none'};background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px;">
                <h3 style="color:var(--text1);font-size:14px;margin-bottom:12px;">Results</h3>
                <div id="backtestStats" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:16px;"></div>
                <div id="backtestTrades" style="overflow-x:auto;"></div>
            </div>
        </div>
    `;
    el.innerHTML = html;
    
    if (results) {
        _displayBacktestResults(results);
    }

    // Resync with backend
    fetch('/api/backtest/status').then(r=>r.json()).then(d=>{
        var runBtn = document.getElementById('backtestRunBtn');
        var status = document.getElementById('backtestStatus');
        if (d.status === 'running') {
            _backtestRunning = true;
            if (runBtn) { runBtn.disabled = true; runBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Running...'; }
            if (status) {
                var pct = d.total > 0 ? Math.round(d.done / d.total * 100) : 0;
                status.innerHTML = '<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Running... ' + d.done + '/' + d.total + ' symbols (' + pct + '%) — ' + (d.current || '') + '</span>';
            }
            _pollBacktestStatus();
        } else if (d.status === 'done' && d.results && !_backtestResults) {
            _backtestResults = d.results;
            if (status) status.innerHTML = '<span style="color:var(--green);">✅ Backtest completed</span>';
            _displayBacktestResults(d.results);
        } else if (d.status === 'error' && !_backtestResults) {
            if (status) status.innerHTML = '<span style="color:var(--red);">❌ ' + (d.error || 'Backtest failed') + '</span>';
        }
    }).catch(()=>{});

    // Event listeners to remember values
    ['backtestWallet','backtestFromDate','backtestToDate','backtestMode','backtestTargetPct','backtestSlPct','backtestMinHoldDays'].forEach(id => {
        var el2 = document.getElementById(id);
        if (el2) {
            el2.addEventListener('change', function() {
                if (id === 'backtestWallet') _backtestWallet = parseFloat(this.value) || 100000;
                else if (id === 'backtestFromDate') _backtestFromDate = this.value;
                else if (id === 'backtestToDate') _backtestToDate = this.value;
                else if (id === 'backtestMode') _backtestMode = this.value;
                else if (id === 'backtestTargetPct') _backtestTargetPct = this.value ? parseFloat(this.value) : null;
                else if (id === 'backtestSlPct') _backtestSlPct = this.value ? parseFloat(this.value) : null;
                else if (id === 'backtestMinHoldDays') _backtestMinHoldDays = this.value !== '' ? parseInt(this.value) : 1;
            });
            el2.addEventListener('input', function() {
                if (id === 'backtestWallet') _backtestWallet = parseFloat(this.value) || 100000;
                else if (id === 'backtestTargetPct') _backtestTargetPct = this.value ? parseFloat(this.value) : null;
                else if (id === 'backtestSlPct') _backtestSlPct = this.value ? parseFloat(this.value) : null;
                else if (id === 'backtestMinHoldDays') _backtestMinHoldDays = this.value !== '' ? parseInt(this.value) : 1;
            });
        }
    });
}

function _displayBacktestResults(res){
    var statsDiv = document.getElementById('backtestStats');
    var tradesDiv = document.getElementById('backtestTrades');
    if (!statsDiv || !tradesDiv) return;
    var modeLabel = res.mode==='DELIVERY' ? 'Delivery (CNC)' : 'Intraday (MIS)';
    statsDiv.innerHTML=`
        <div style="background:var(--bg2);padding:8px;border-radius:6px;text-align:center;"><span style="color:var(--text3);">Total Trades</span><br><span style="font-family:Space Mono;font-size:16px;">${res.total_trades}</span></div>
        <div style="background:var(--bg2);padding:8px;border-radius:6px;text-align:center;"><span style="color:var(--text3);">Win Rate</span><br><span style="font-family:Space Mono;font-size:16px;color:${res.win_rate>=50?'var(--green)':'var(--red)'};">${res.win_rate}%</span></div>
        <div style="background:var(--bg2);padding:8px;border-radius:6px;text-align:center;"><span style="color:var(--text3);">Net P&L</span><br><span style="font-family:Space Mono;font-size:16px;color:${res.net_pnl>=0?'var(--green)':'var(--red)'};">${res.net_pnl>=0?'+':''}₹${res.net_pnl.toFixed(2)}</span></div>
        <div style="background:var(--bg2);padding:8px;border-radius:6px;text-align:center;"><span style="color:var(--text3);">Final Wallet</span><br><span style="font-family:Space Mono;font-size:16px;color:var(--gold);">₹${res.final_wallet.toFixed(2)}</span></div>
    `;
    var mhdBadge = (res.mode==='DELIVERY' && res.min_hold_days!=null) ? ' <span class="b bn">Min Hold: '+res.min_hold_days+'d</span>' : '';
    var modeBadge = '<div style="margin-bottom:10px;font-size:10px;color:var(--text3);font-family:Space Mono,monospace">Mode: <span class="b bg-gold">'+modeLabel+'</span>'+mhdBadge+'</div>';
    function _fmtDateTime(iso){
        if(!iso) return '—';
        var d = new Date(iso);
        if(isNaN(d.getTime())){
            var s = String(iso);
            return s.length>=16 ? s.slice(0,10)+' '+s.slice(11,16) : (s || '—');
        }
        var pad=n=>String(n).padStart(2,'0');
        return d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate())+' '+pad(d.getHours())+':'+pad(d.getMinutes());
    }
    if(res.trades && res.trades.length){
        var html=modeBadge+'<table style="width:100%;font-size:11px;"><thead><tr><th>Symbol</th><th>Side</th><th>Entry Date/Time</th><th>Entry</th><th>Exit Date/Time</th><th>Exit</th><th>Held</th><th>Qty</th><th>Target</th><th>SL</th><th>P&L</th><th>Reason</th></tr></thead><tbody>';
        res.trades.forEach(t=>{
            var entryDt = new Date(t.entry_time);
            var exitDt = new Date(t.exit_time);
            var heldTxt = '—';
            if(!isNaN(entryDt.getTime()) && !isNaN(exitDt.getTime())){
                var ms = exitDt - entryDt;
                var days = Math.floor(ms/86400000);
                var hrs = Math.floor((ms%86400000)/3600000);
                heldTxt = days>0 ? days+'d '+hrs+'h' : hrs+'h';
            }
            var tgtTxt = (t.target!=null) ? '₹'+Number(t.target).toFixed(2) : '—';
            var slTxt = (t.stoploss!=null) ? '₹'+Number(t.stoploss).toFixed(2) : '—';
            html+=`<tr><td class="sym">${t.symbol}</td><td><span class="b ${t.side==='BUY'?'bb':'bs'}">${t.side}</span></td><td class="num">${_fmtDateTime(t.entry_time)}</td><td class="num">₹${t.entry_price.toFixed(2)}</td><td class="num">${_fmtDateTime(t.exit_time)}</td><td class="num">₹${t.exit_price.toFixed(2)}</td><td class="num" style="color:var(--text3)">${heldTxt}</td><td class="num">${t.qty}</td><td class="num" style="color:var(--green-b)">${tgtTxt}</td><td class="num" style="color:var(--red-b)">${slTxt}</td><td class="${t.pnl>=0?'pos':'neg'}">${t.pnl>=0?'+':''}₹${t.pnl.toFixed(2)}</td><td><span class="b bg-gold">${t.exit_reason}</span></td></tr>`;
        });
        html+='</tbody></table>';
        tradesDiv.innerHTML=html;
    } else {
        tradesDiv.innerHTML=modeBadge+'<p style="color:var(--text3);font-size:12px;">No trades executed.</p>';
    }
    document.getElementById('backtestResults').style.display='block';
}

function runBacktest(){
    var wallet = parseFloat(document.getElementById('backtestWallet').value) || 100000;
    var fromDate = document.getElementById('backtestFromDate').value;
    var toDate = document.getElementById('backtestToDate').value;
    var mode = document.getElementById('backtestMode') ? document.getElementById('backtestMode').value : (CURRENT_MODE||'INTRADAY');
    var targetPct = document.getElementById('backtestTargetPct').value;
    var slPct = document.getElementById('backtestSlPct').value;
    var minHoldDaysInput = document.getElementById('backtestMinHoldDays') ? document.getElementById('backtestMinHoldDays').value : '1';
    var status = document.getElementById('backtestStatus');
    var runBtn = document.getElementById('backtestRunBtn');
    if (!fromDate || !toDate) {
        status.innerHTML='<span style="color:var(--orange);">Please select both From and To dates.</span>';
        return;
    }
    if (fromDate > toDate) {
        status.innerHTML='<span style="color:var(--red);">From date must be before To date.</span>';
        return;
    }
    _backtestRunning = true;
    _backtestResults = null;
    _backtestMode = mode;
    if (targetPct !== '') _backtestTargetPct = parseFloat(targetPct);
    else _backtestTargetPct = null;
    if (slPct !== '') _backtestSlPct = parseFloat(slPct);
    else _backtestSlPct = null;
    _backtestMinHoldDays = minHoldDaysInput !== '' ? parseInt(minHoldDaysInput) : 1;
    if (runBtn) { runBtn.disabled = true; runBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Running...'; }
    status.innerHTML='<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Starting backtest...</span>';
    document.getElementById('backtestResults').style.display='none';
    
    var payload = {wallet: wallet, from_date: fromDate, to_date: toDate, mode: mode, min_hold_days: _backtestMinHoldDays};
    if (_backtestTargetPct !== null) payload.target_pct = _backtestTargetPct;
    if (_backtestSlPct !== null) payload.stoploss_pct = _backtestSlPct;
    
    fetch('/api/backtest/run', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload)
    })
    .then(r=>r.json())
    .then(d=>{
        if(d.status==='error'){
            _backtestRunning = false;
            if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="fas fa-play"></i> Run Backtest'; }
            status.innerHTML='<span style="color:var(--red);">❌ '+d.msg+'</span>';
            _backtestResults = null;
            return;
        }
        status.innerHTML='<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Running backtest...</span>';
        _pollBacktestStatus();
    })
    .catch(e=>{
        _backtestRunning = false;
        if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="fas fa-play"></i> Run Backtest'; }
        status.innerHTML='<span style="color:var(--red);">❌ Network error: '+e.message+'</span>';
        _backtestResults = null;
    });
}

function _pollBacktestStatus(){
    if (_backtestPollTimer) { clearInterval(_backtestPollTimer); _backtestPollTimer = null; }
    _backtestPollTimer = setInterval(function(){
        fetch('/api/backtest/status').then(r=>r.json()).then(d=>{
            var status = document.getElementById('backtestStatus');
            var runBtn = document.getElementById('backtestRunBtn');
            if (!status) { clearInterval(_backtestPollTimer); _backtestPollTimer = null; return; }
            if (d.status === 'running') {
                var pct = d.total > 0 ? Math.round(d.done / d.total * 100) : 0;
                status.innerHTML = '<span style="color:var(--blue);"><i class="fas fa-spinner fa-spin"></i> Running... ' + d.done + '/' + d.total + ' symbols (' + pct + '%) — ' + (d.current || '') + '</span>';
            } else if (d.status === 'done') {
                clearInterval(_backtestPollTimer); _backtestPollTimer = null;
                _backtestRunning = false;
                if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="fas fa-play"></i> Run Backtest'; }
                _backtestResults = d.results;
                status.innerHTML = '<span style="color:var(--green);">✅ Backtest completed</span>';
                _displayBacktestResults(d.results);
            } else if (d.status === 'error') {
                clearInterval(_backtestPollTimer); _backtestPollTimer = null;
                _backtestRunning = false;
                if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = '<i class="fas fa-play"></i> Run Backtest'; }
                status.innerHTML = '<span style="color:var(--red);">❌ ' + (d.error || 'Backtest failed') + '</span>';
            } else if (d.status === 'idle') {
                clearInterval(_backtestPollTimer); _backtestPollTimer = null;
            }
        }).catch(()=>{});
    }, 2000);
}

// ─── OVERVIEW ──────────────────────────────────────────────
function renderPTOverview(el){
var pos=ptData.positions||{};var syms=Object.keys(pos);
var tp=ptData.target_pct||0.8,sl=ptData.sl_pct||0.5;
if(!syms.length){
    el.innerHTML='<div class="es"><i class="fas fa-robot"></i><p>No open positions.<br><small>Monitoring <b>'+(ptData.pinned_count||0)+'</b> stocks · Target <span style="color:var(--green)">+'+tp.toFixed(1)+'%</span> · SL <span style="color:var(--red)">-'+sl.toFixed(1)+'%</span><br><span style="color:var(--red);font-size:10px">P&L shown NET of charges</span></small></p></div>';
    return;
}
var html='<div class="section-header"><span class="section-title">Open Positions — Live MTM (Net)</span><span style="font-size:10px;color:var(--text3)">Target +'+tp.toFixed(1)+'%  ·  SL -'+sl.toFixed(1)+'%</span></div>';
syms.forEach(sym=>{
    var p=pos[sym],pnl=p.upnl||0,ltp=p.ltp||p.pos.entry_price;
    var tgt=p.target||0,slv=p.stoploss||0,entry=p.pos.entry_price;
    var cls=pnl>0?'pos':'neg',estChg=p.est_charges||0;
    var progPct=0;
    if(tgt&&entry&&tgt!==entry)
    progPct=p.pos.side==='BUY'?Math.max(0,Math.min(100,(ltp-entry)/(tgt-entry)*100)):Math.max(0,Math.min(100,(entry-ltp)/(entry-tgt)*100));
    var progColor=pnl>=0?'var(--green-b)':'var(--red-b)';
    html+='<div class="pos-card" style="flex-direction:column;align-items:stretch;gap:6px">'
    +'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
    +'<span class="pos-sym">'+sym+'</span>'
    +'<span class="pos-side '+(p.pos.side==='BUY'?'pos-buy':'pos-sell')+'">'+p.pos.side+'</span>'
    +'<span class="num">'+p.pos.qty+' qty</span>'
    +'<span class="num" style="color:var(--text3)">Entry ₹'+Number(entry).toFixed(2)+'</span>'
    +'<span class="num" style="color:var(--gold)">LTP ₹'+Number(ltp).toFixed(2)+'</span>'
    +'<span style="font-size:9px;color:var(--red)">Est.Chg ₹'+estChg.toFixed(2)+'</span>'
    +'<span class="pos-pnl '+cls+'" style="margin-left:auto">'+(pnl>=0?'+':'')+'₹'+Math.abs(pnl).toFixed(2)+' (NET)</span>'
    +'<span style="font-size:9px;color:var(--text3)">🎯₹'+Number(tgt).toFixed(2)+'  🛑₹'+Number(slv).toFixed(2)+'</span>'
    +'<button class="btn btn-r" style="padding:3px 9px;font-size:10px" onclick="forceExit(\''+sym+'\')"><i class="fas fa-xmark"></i> Exit</button>'
    +'</div>'
    +'<div style="height:4px;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden">'
    +'<div style="height:100%;width:'+progPct.toFixed(1)+'%;background:'+progColor+';transition:width .5s;border-radius:2px"></div>'
    +'</div></div>';
});
el.innerHTML=html;
}

function renderPTPositions(el){
var pos=ptData.positions||{};var syms=Object.keys(pos);
if(!syms.length){el.innerHTML='<div class="es"><i class="fas fa-inbox"></i><p>No open positions</p></div>';return;}
var html='<div class="tw">\n<table>\n<thead>\n<tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>LTP</th><th>Net P&L</th><th>P&L%</th><th>Est.Chg</th><th>Leverage</th><th>🎯 Target</th><th>🛑 SL</th><th>Action</th></tr>\n</thead>\n<tbody>';
syms.forEach(sym=>{
    var p=pos[sym],pnl=p.upnl||0,pct=p.pct||0,ltp=p.ltp||p.pos.entry_price,estChg=p.est_charges||0;
    var lev=p.pos.leverage||5; var mPct=p.pos.margin_pct!=null?p.pos.margin_pct:0.20;
    var mSrc=p.pos.margin_source||'fallback';
    var levColor=lev>=5?'var(--green)':lev>=3?'var(--gold)':'var(--text2)';
    var mSrcBadge=mSrc==='kite_api'?'<span style="font-size:8px;color:var(--blue);opacity:.7">API</span>':'<span style="font-size:8px;color:var(--text3);opacity:.7">est</span>';
    html+='<tr>'
    +'<td class="sym">'+sym+'</td>'
    +'<td><span class="b '+(p.pos.side==='BUY'?'bb':'bs')+'">'+p.pos.side+'</span></td>'
    +'<td class="num">'+p.pos.qty+'</td>'
    +'<td class="num">₹'+Number(p.pos.entry_price).toFixed(2)+'</td>'
    +'<td class="num" style="color:var(--gold)">₹'+Number(ltp).toFixed(2)+'</td>'
    +'<td class="'+(pnl>=0?'pos':'neg')+'">'+(pnl>=0?'+':'')+'₹'+Math.abs(pnl).toFixed(2)+'</td>'
    +'<td class="'+(pct>=0?'pos':'neg')+'">'+(pct>=0?'+':'')+pct.toFixed(2)+'%</td>'
    +'<td class="num" style="color:var(--red)">₹'+estChg.toFixed(2)+'</td>'
    +'<td class="num"><span style="color:'+levColor+';font-weight:700">'+lev.toFixed(1)+'x</span><br>'
        +'<span style="font-size:9px;color:var(--text3)">'+(mPct*100).toFixed(0)+'%</span> '+mSrcBadge+'</td>'
    +'<td class="num" style="color:var(--green-b)">₹'+Number(p.target||0).toFixed(2)+'</td>'
    +'<td class="num" style="color:var(--red-b)">₹'+Number(p.stoploss||0).toFixed(2)+'</td>'
    +'<td><button class="btn btn-r" style="padding:3px 9px;font-size:10px" onclick="forceExit(\''+sym+'\')">Exit</button></td>'
    +'</tr>';
});
html+='</tbody>\n</table>\n</div>';el.innerHTML=html;
}

function renderPTOrders(el){
fetch('/paper/orders').then(r=>r.json()).then(d=>{
    var orders=(d.orders||[]).slice().reverse().slice(0,100);
    if(!orders.length){el.innerHTML='<div class="es"><i class="fas fa-receipt"></i><p>No orders yet</p></div>';return;}
    var html='<div class="tw">\n<table>\n<thead>\n<tr><th>Time</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Value</th><th>Brokerage</th><th>STT</th><th>Total Chg</th><th>Reason</th></tr>\n</thead>\n<tbody>';
    orders.forEach(o=>{
    html+='<tr><td class="num" style="font-size:10px">'+o.time+'</td><td class="sym">'+o.symbol+'</td>'
        +'<td class="'+(o.side==='BUY'?'pos':'neg')+'">'+o.side+'</td>'
        +'<td class="num">'+o.qty+'</td><td class="num">₹'+Number(o.price).toFixed(2)+'</td>'
        +'<td class="num">₹'+Number(o.value).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(o.brokerage||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(o.stt||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red);font-weight:700">₹'+Number(o.total_charges||0).toFixed(2)+'</td>'
        +'<td><span class="b bg-gold">'+o.reason+'</span></td></tr>';
    });
    html+='</tbody>\n</table>\n</div>';el.innerHTML=html;
});
}

function renderPTTrades(el){
fetch('/paper/trades').then(r=>r.json()).then(d=>{
    var trades=(d.trades||[]).slice().reverse();
    if(!trades.length){el.innerHTML='<div class="es"><i class="fas fa-chart-line"></i><p>No closed trades yet</p></div>';return;}
    var tNet=trades.reduce((a,t)=>a+t.pnl,0);
    var tGross=trades.reduce((a,t)=>a+(t.gross_pnl||t.pnl),0);
    var tChg=trades.reduce((a,t)=>a+(t.total_charges||0),0);
    var html='<div class="charges-box"><div class="charges-box-title">📊 P&L Summary</div>'
    +'<div class="charges-row"><span>Gross P&L</span><span style="color:'+(tGross>=0?'var(--green)':'var(--red)')+'">₹'+tGross.toFixed(2)+'</span></div>'
    +'<div class="charges-row"><span>Total Charges</span><span style="color:var(--red)">-₹'+tChg.toFixed(2)+'</span></div>'
    +'<div class="charges-row '+(tNet>=0?'net-pos':'net-neg')+'"><span>NET P&L</span><span>'+(tNet>=0?'+':'')+'₹'+tNet.toFixed(2)+'</span></div></div>';
    html+='<div class="tw">\n<table>\n<thead>\n<tr><th>Date</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>Gross</th><th>Brok</th><th>STT</th><th>Exch</th><th>GST</th><th>Stamp</th><th>Total Chg</th><th>NET P&L</th><th>NET%</th><th>Reason</th></tr>\n</thead>\n<tbody>';
    trades.forEach(t=>{
    var gc=t.gross_pnl>=0?'pos':'neg',nc=t.pnl>=0?'pos':'neg';
    html+='<tr><td class="num" style="font-size:10px">'+t.date+'</td><td class="sym">'+t.symbol+'</td>'
        +'<td><span class="b '+(t.side==='BUY'?'bb':'bs')+'">'+t.side+'</span></td>'
        +'<td class="num">'+t.qty+'</td>'
        +'<td class="num">₹'+Number(t.entry_price).toFixed(2)+'</td>'
        +'<td class="num">₹'+Number(t.exit_price).toFixed(2)+'</td>'
        +'<td class="'+gc+'">'+(t.gross_pnl>=0?'+':'')+'₹'+Math.abs(t.gross_pnl||t.pnl).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(t.brokerage||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(t.stt||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(t.exchange_charge||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(t.gst||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red)">₹'+Number(t.stamp_duty||0).toFixed(2)+'</td>'
        +'<td class="num" style="color:var(--red);font-weight:700">₹'+Number(t.total_charges||0).toFixed(2)+'</td>'
        +'<td class="'+nc+'" style="font-weight:700">'+(t.pnl>=0?'+':'')+'₹'+Math.abs(t.pnl).toFixed(2)+'</td>'
        +'<td class="'+nc+'">'+(t.pnl_pct>=0?'+':'')+t.pnl_pct+'%</td>'
        +'<td><span class="b bg-gold">'+t.exit_reason+'</span></td>'
        +'</tr>';
    });
    html+='</tbody>\n</table>\n</div>';el.innerHTML=html;
});
}

function renderPTDaily(el){
var daily=ptData.daily_pnl||{};var dates=Object.keys(daily).sort().reverse();
if(!dates.length){el.innerHTML='<div class="es"><i class="fas fa-calendar"></i><p>No daily data</p></div>';return;}
var tNet=Object.values(daily).reduce((a,d)=>a+(d.realized||0),0);
var tGross=Object.values(daily).reduce((a,d)=>a+(d.gross_realized||0),0);
var tChg=Object.values(daily).reduce((a,d)=>a+(d.total_charges||0),0);
var html='<div class="charges-box"><div class="charges-box-title">📅 Summary</div>'
    +'<div class="charges-row"><span>Gross Realized</span><span style="color:'+(tGross>=0?'var(--green)':'var(--red)')+'">₹'+tGross.toFixed(2)+'</span></div>'
    +'<div class="charges-row"><span>Total Charges</span><span style="color:var(--red)">-₹'+tChg.toFixed(2)+'</span></div>'
    +'<div class="charges-row '+(tNet>=0?'net-pos':'net-neg')+'"><span>NET</span><span>'+(tNet>=0?'+':'')+'₹'+tNet.toFixed(2)+'</span></div></div>';
html+='<div class="tw">\n<table>\n<thead>\n<tr><th>Date</th><th>Gross</th><th>Charges</th><th>Net P&L</th><th>Trades</th><th>W/L</th><th>Status</th></tr>\n</thead>\n<tbody>';
dates.forEach(dt=>{
    var d=daily[dt],r=d.realized||0,gross=d.gross_realized||r,chg=d.total_charges||0;
    html+='<tr><td class="num">'+dt+'</td>'
    +'<td class="'+(gross>=0?'pos':'neg')+'">'+(gross>=0?'+':'')+'₹'+Math.abs(gross).toFixed(2)+'</td>'
    +'<td class="num" style="color:var(--red)">₹'+chg.toFixed(2)+'</td>'
    +'<td class="'+(r>=0?'pos':'neg')+'" style="font-weight:700">'+(r>=0?'+':'')+'₹'+Math.abs(r).toFixed(2)+'</td>'
    +'<td class="num">'+d.trades+'</td>'
    +'<td class="num"><span style="color:var(--green)">'+(d.wins||0)+'W</span>/<span style="color:var(--red)">'+(d.losses||0)+'L</span></td>'
    +'<td><span class="pnl-pill '+(r>0?'pnl-pos':r<0?'pnl-neg':'pnl-zero')+'">'+(r>0?'PROFIT':r<0?'LOSS':'FLAT')+'</span></td></tr>';
});
html+='</tbody>\n</table>\n</div>';el.innerHTML=html;
}

function initWishlistSearch(){
var inp=document.getElementById('wishlistSearch');if(!inp)return;
inp.addEventListener('input',function(){
    var q=inp.value.trim().toUpperCase();var dd=document.getElementById('wishlistDropdown');
    if(!q){dd.style.display='none';return;}
    var m=ALL_SYMS.filter(s=>s.indexOf(q)===0).slice(0,12);
    if(!m.length){dd.style.display='none';return;}
    dd.innerHTML=m.map(s=>'<div class="wishlist-item" onclick="addToWishlist(\''+s+'\')">'
    +'<span>'+s+'</span><span class="wishlist-item-add">'+(isPinned(s)?'✓ PINNED':'➕ Add')+'</span></div>').join('');
    dd.style.display='block';
});
inp.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('wishlistDropdown').style.display='none';});
document.addEventListener('click',e=>{if(!e.target.closest('.wishlist-search-input'))document.getElementById('wishlistDropdown').style.display='none';});
}
function addToWishlist(sym){
document.getElementById('wishlistDropdown').style.display='none';
document.getElementById('wishlistSearch').value='';
if(!isPinned(sym)){
    fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,action:'pin'})})
    .then(r=>r.json()).then(d=>{if(d.status==='ok'){PINNED_SYMS.push(sym);showToast('📌 '+sym+' added');loadPTData();}});
} else showToast('⚠️ '+sym+' already monitored');
}

function renderPTPinned(el){
var pinned=ptData.pinned_list||[];
var existingPanel=document.getElementById('fullScanPanel');
if(existingPanel){
    var countEl=document.getElementById('pinnedCountStat');
    if(countEl)countEl.textContent=pinned.length;
    var chipsWrap=document.getElementById('pinnedChipsWrap');
    if(chipsWrap){
    var newChips=document.createElement('div');
    newChips.innerHTML=_buildPinnedChips(pinned, ptData.pinned_meta||{});
    var built=newChips.firstElementChild;
    if(built)chipsWrap.parentNode.replaceChild(built,chipsWrap);
    }
    return;
}
var searchHtml='<div class="wishlist-search"><div class="wishlist-search-title">🔍 Add to Monitored List</div>'
    +'<div style="position:relative;"><div class="wishlist-search-input">'
    +'<i class="fas fa-magnifying-glass wishlist-search-icon"></i>'
    +'<input class="wishlist-search-field" id="wishlistSearch" type="text" placeholder="Search any NSE stock..." autocomplete="off">'
    +'<div class="wishlist-dropdown" id="wishlistDropdown"></div>'
    +'</div></div>'
    +'<div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">'
    +'<span style="font-size:10px;color:var(--text3);font-family:Space Mono,monospace">Monitored: <span style="color:var(--gold)" id="pinnedCountStat">'+pinned.length+'</span></span>'
    +'</div></div>';
var scanSection='<div style="background:var(--bg1);border:1px solid rgba(88,166,255,.25);border-radius:9px;padding:14px;margin-bottom:12px" id="fullScanPanel">'
    +'<div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:8px">'
    +'<div><div style="font-family:Space Mono,monospace;font-size:12px;font-weight:700;color:var(--blue)">🔎 Full Market Scanner</div>'
    +'<div style="font-size:10px;color:var(--text3);margin-top:3px">5-min + 15-min MTF scan · Best windows: <span style="color:var(--gold)">8:45–9:10 AM</span> pre-market · <span style="color:var(--gold)">9:30–10:00 AM</span> post-open · Trading window: <span style="color:var(--gold)">9:15 AM – 2:00 PM</span></div></div>'
    +'<div style="display:flex;gap:6px;flex-wrap:wrap">'
    +'<button class="btn btn-orange" onclick="loadMovers()" style="font-size:11px;padding:5px 10px"><i class="fas fa-fire"></i> Market Movers</button>'
    +'<button class="btn btn-p" id="startScanBtn" onclick="startFullScan()"><i class="fas fa-radar"></i> Start Scan</button>'
    +'<button class="btn btn-r" id="stopScanBtn" onclick="stopFullScan()" style="display:none"><i class="fas fa-stop"></i> Stop</button>'
    +'</div></div>'
    +'<div id="moversPanel" style="display:none;margin-bottom:10px">'
    +'<div style="display:flex;gap:4px;margin-bottom:8px;overflow-x:auto;padding-bottom:2px">'
    +'<button class="btn btn-g mover-tab" id="mtab_gainers" onclick="switchMoverTab(this,\'gainers\')" style="font-size:10px;padding:4px 10px">\ud83d\udcc8 Gainers</button>'
    +'<button class="btn btn-gh mover-tab" id="mtab_losers" onclick="switchMoverTab(this,\'losers\')" style="font-size:10px;padding:4px 10px">\ud83d\udcc9 Losers</button>'
    +'<button class="btn btn-gh mover-tab" id="mtab_vol_gainers" onclick="switchMoverTab(this,\'vol_gainers\')" style="font-size:10px;padding:4px 10px">\ud83d\udd25 Vol Surge</button>'
    +'<button class="btn btn-gh mover-tab" id="mtab_momentum" onclick="switchMoverTab(this,\'momentum\')" style="font-size:10px;padding:4px 10px">\u26a1 Momentum</button>'
    +'</div>'
    +'<div id="moversContent"><div class="es"><div class="spin"></div><p>Loading...</p></div></div>'
    +'</div>'
    +'<div class="scan-config-grid">'
    +'<div style="display:flex;flex-direction:column;gap:4px"><label style="font-size:9px;color:var(--text3);font-family:Space Mono,monospace">SCAN MODE</label>'
    +'<select id="scanMode" style="width:100%"><option value="nifty200" selected>⭐ Nifty 200 (~200 stocks, ~2.5 min) RECOMMENDED</option><option value="priority">Priority (NIFTY + Key stocks first)</option><option value="top200">Top 200 Liquid</option><option value="nifty_indices">NIFTY Indices Only (~150)</option><option value="all">All NSE (~1800+, ~2 hrs)</option></select></div>'
    +'<div style="display:flex;flex-direction:column;gap:4px"><label style="font-size:9px;color:var(--text3);font-family:Space Mono,monospace">MAX STOCKS</label>'
    +'<input type="number" id="scanMaxStocks" value="1800" min="50" max="2000" step="50" style="width:100%"></div>'
    +'<div style="display:flex;flex-direction:column;gap:4px"><label style="font-size:9px;color:var(--text3);font-family:Space Mono,monospace">MIN SCORE</label>'
    +'<input type="number" id="scanMinScore" value="25" min="10" max="100" step="5" style="width:100%"></div></div>'
    +'<div id="scanProgressWrap" style="display:none;margin-top:10px">'
    +'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">'
    +'<span id="scanStatusText" style="font-size:10px;color:var(--text2);font-family:Space Mono,monospace">Scanning...</span>'
    +'<span id="scanCountText" style="font-size:10px;color:var(--text3);font-family:Space Mono,monospace">0/0</span></div>'
    +'<div class="scan-progress-bar"><div class="scan-progress-fill" id="scanProgressFill" style="width:0%"></div></div>'
    +'<div id="scanCurrentSym" style="font-size:9px;color:var(--text3);font-family:Space Mono,monospace;margin-top:3px"></div></div>'
    +'<div id="pinnableSuggestions" style="color:var(--text3);font-size:11px;text-align:center;padding:10px 0;margin-top:8px">'
    +'<i class="fas fa-info-circle" style="opacity:.4;margin-right:5px"></i>Click <b>Start Scan</b> for intraday candidates · <b>Market Movers</b> for live gainers/losers/volume.'
    +'</div></div>';
el.innerHTML=searchHtml+scanSection+_buildPinnedChips(pinned, ptData.pinned_meta||{});
initWishlistSearch();_pinnableSuggestionsLoaded=false;
pollScanStatus();
}

function startFullScan(){
var modeEl=document.getElementById('scanMode');
var mode=modeEl?modeEl.options[modeEl.selectedIndex].value:'all';
var maxStocks=document.getElementById('scanMaxStocks')?.value||1800;
var minScore=document.getElementById('scanMinScore')?.value||25;
fetch('/scanner/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:mode,max_stocks:parseInt(maxStocks),min_score:parseFloat(minScore)})})
.then(r=>r.json()).then(d=>{
    if(d.status==='started'||d.status==='already_running'){
    showToast('🔎 Scan started!');
    document.getElementById('startScanBtn').style.display='none';
    document.getElementById('stopScanBtn').style.display='inline-flex';
    document.getElementById('scanProgressWrap').style.display='block';
    if(_scanPollingTimer)clearInterval(_scanPollingTimer);
    _scanPollingTimer=setInterval(pollScanStatus,1500);
    } else showToast('❌ '+(d.msg||'Error'));
}).catch(e=>showToast('❌ '+e.message));
}
function stopFullScan(){
fetch('/scanner/stop',{method:'POST'}).then(()=>{showToast('⏹ Scan stopped');});
if(_scanPollingTimer){clearInterval(_scanPollingTimer);_scanPollingTimer=null;}
document.getElementById('startScanBtn').style.display='inline-flex';
document.getElementById('stopScanBtn').style.display='none';
}
function pollScanStatus(){
fetch('/scanner/status').then(r=>r.json()).then(d=>{
    var wrap=document.getElementById('scanProgressWrap');
    var fillEl=document.getElementById('scanProgressFill');
    var statusEl=document.getElementById('scanStatusText');
    var countEl=document.getElementById('scanCountText');
    var curEl=document.getElementById('scanCurrentSym');
    var startBtn=document.getElementById('startScanBtn');
    var stopBtn=document.getElementById('stopScanBtn');
    if(!wrap)return;

    if(d.status==='running'){
    wrap.style.display='block';
    if(startBtn)startBtn.style.display='none';
    if(stopBtn)stopBtn.style.display='inline-flex';
    var pct=d.total>0?Math.round(d.done/d.total*100):0;
    if(fillEl)fillEl.style.width=pct+'%';
    if(statusEl)statusEl.textContent='Scanning... '+pct+'% ('+(d.elapsed||0)+'s)';
    if(countEl)countEl.textContent=d.done+'/'+d.total+' · '+d.found+' found · '+d.errors+' errors';
    if(curEl)curEl.textContent='→ '+d.current;
    if(!_scanPollingTimer)_scanPollingTimer=setInterval(pollScanStatus,1500);
    } else if(d.status==='done'){
    wrap.style.display='block';
    if(fillEl)fillEl.style.width='100%';
    if(startBtn)startBtn.style.display='inline-flex';
    if(stopBtn)stopBtn.style.display='none';
    if(statusEl)statusEl.textContent='✅ Scan complete — '+d.found+' candidates found in '+d.elapsed+'s';
    if(countEl)countEl.textContent=d.done+'/'+d.total+' scanned · '+d.errors+' errors';
    if(curEl)curEl.textContent='';
    if(_scanPollingTimer){clearInterval(_scanPollingTimer);_scanPollingTimer=null;}
    _pinnableSuggestionsLoaded=false;
    loadScanResults();
    } else {
    if(startBtn)startBtn.style.display='inline-flex';
    if(stopBtn)stopBtn.style.display='none';
    if(d.last_scan&&d.found>0){
        wrap.style.display='block';
        if(statusEl)statusEl.textContent='Last scan: '+new Date(d.last_scan).toLocaleString('en-IN');
        if(countEl)countEl.textContent=d.found+' candidates';
        if(fillEl)fillEl.style.width='100%';
        if(!_pinnableSuggestionsLoaded)loadScanResults();
    } else wrap.style.display='none';
    }
}).catch(()=>{});
}
function loadScanResults(){
var box=document.getElementById('pinnableSuggestions');if(!box)return;
box.innerHTML='<div style="display:flex;align-items:center;justify-content:center;gap:8px;padding:15px;color:var(--blue)"><div class="spin"></div><span>Loading results...</span></div>';
fetch('/paper/scan-pinnable').then(r=>r.json()).then(d=>{
    if(!d.stocks||!d.stocks.length){
    var minS=d.min_score!=null?d.min_score:'—';
    var tot=d.total_found||0;
    var msg=tot>0
        ?'<b>'+tot+' stocks</b> passed score≥'+minS+' but none had a clear BUY/SELL direction (all NEUTRAL). Lower Min Score or run a fresh scan.'
        :'No candidates found. Try lowering Min Score (currently '+minS+') or run a fresh scan.';
    box.innerHTML='<div style="color:var(--text3);font-size:11px;text-align:center;padding:15px"><i class="fas fa-info-circle" style="opacity:.4;margin-right:5px"></i>'+msg+'</div>';
    _pinnableSuggestionsLoaded=false;return;
    }
    _pinnableSuggestionsLoaded=true;
    var upc=d.stocks.filter(s=>!s.already_pinned).length;
    var pc=d.stocks.filter(s=>s.already_pinned).length;
    var totalScanned=d.total_scanned||0;
    var totalFound=d.total_found||d.count;
    var minScoreUsed=d.min_score!=null?d.min_score:'—';
    var toolbar='<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:10px 12px;background:rgba(88,166,255,.05);border:1px solid rgba(88,166,255,.15);border-radius:8px;margin-bottom:10px">'
    +'<span style="font-size:10px;color:var(--text3);font-family:Space Mono,monospace">BULK:</span>'
    +'<button class="btn btn-gold" id="pinAllBtn" onclick="pinAllSuggestions()" style="font-size:11px;padding:4px 12px"'+(upc===0?' disabled':'')+'><i class="fas fa-thumbtack"></i> Pin All'+(upc>0?' <span style="opacity:.7">('+upc+')</span>':'')+'</button>'
    +'<button class="btn btn-r" id="unpinAllBtn" onclick="unpinAllSuggestions()" style="font-size:11px;padding:4px 12px"'+(pc===0?' disabled':'')+'><i class="fas fa-thumbtack fa-flip-horizontal"></i> Unpin All'+(pc>0?' <span style="opacity:.7">('+pc+')</span>':'')+'</button>'
    +'<span style="margin-left:auto;font-size:10px;color:var(--text3);font-family:Space Mono,monospace">'
    +'<span style="color:var(--blue)">'+d.count+' actionable</span>'
    +' / '+totalFound+' passed score≥'+minScoreUsed
    +' / '+totalScanned+' scanned'
    +(pc>0?' · <span style="color:var(--gold)">'+pc+' pinned</span>':'')
    +'</span></div>';
    var table='<div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;table-layout:fixed">'
    +'<colgroup>'
    +'<col style="width:90px">'
    +'<col style="width:80px">'
    +'<col style="width:62px">'
    +'<col style="width:90px">'
    +'<col style="width:62px">'
    +'<col style="width:62px">'
    +'<col style="width:62px">'
    +'<col style="width:44px">'
    +'<col style="width:50px">'
    +'<col style="width:50px">'
    +'<col style="width:260px">'
    +'<col style="width:80px">'
    +'</colgroup>'
    +'<thead><tr style="background:var(--bg2);border-bottom:2px solid var(--border)">'
    +['SYMBOL','PRICE','CHG%','GAP%','VOL','DIRECTION','SCORE','5M SCR','15M SCR','MTF','RSI','ADX','STRATEGIES','ACTION']
    .map(h=>'<th style="padding:7px 8px;text-align:left;font-size:9px;font-family:Space Mono,monospace;font-weight:700;color:var(--text3);white-space:nowrap">'+h+'</th>').join('')
    +'</tr></thead><tbody id="suggTableBody">';
    d.stocks.forEach(s=>{table+=_suggRow(s);});
    table+='</tbody></table></div><div style="font-size:9px;color:var(--text3);margin-top:8px;font-family:Space Mono,monospace">Results scored on 5-min + 15-min candles · MTF = Multi-timeframe alignment ✅/⚠️</div>';
    box.innerHTML=toolbar+table;
}).catch(()=>{
    if(box)box.innerHTML='<div style="color:var(--red);font-size:11px;text-align:center;padding:10px">Error loading results. Try again.</div>';
    _pinnableSuggestionsLoaded=false;
});
}

var _moversData={};var _activeMoverTab='gainers';
function loadMovers(){
var mp=document.getElementById('moversPanel');if(!mp)return;
mp.style.display='block';
var mc=document.getElementById('moversContent');if(mc)mc.innerHTML='<div style="display:flex;align-items:center;gap:8px;padding:14px;color:var(--blue)"><div class="spin"></div><span style="font-size:11px">Fetching movers...</span></div>';
fetch('/market/movers').then(r=>r.json()).then(d=>{
    _moversData=d;
    var g=d.gainers||[];var l=d.losers||[];
    var dt=g.length>=l.length?'gainers':'losers';
    var btn=document.getElementById('mtab_'+dt);
    switchMoverTab(btn,dt);
    if(d.as_of)showToast('Movers updated '+d.as_of);
}).catch(()=>{var mc=document.getElementById('moversContent');if(mc)mc.innerHTML='<div style="color:var(--red);padding:10px;font-size:11px">Error loading movers.</div>';});
}
function switchMoverTab(btn,tab){
_activeMoverTab=tab;
document.querySelectorAll('.mover-tab').forEach(b=>{b.className='btn btn-gh mover-tab';});
if(btn){var tc={'gainers':'g','losers':'r','vol_gainers':'orange','momentum':'p'};btn.className='btn btn-'+(tc[tab]||'g')+' mover-tab';}
renderMovers(tab);
}
function renderMovers(tab){
var mc=document.getElementById('moversContent');if(!mc||!_moversData[tab])return;
var rows=_moversData[tab];
if(!rows.length){mc.innerHTML='<div style="color:var(--text3);font-size:11px;padding:10px;text-align:center">No '+tab.replace('_',' ')+' found</div>';return;}
var headers=['SYMBOL','LTP','CHG%','VOL RATIO','RSI','DAY HIGH','DAY LOW','ACTION'];
var html='<div class="tw">\n<table>\n<thead>\n<tr>'+headers.map(h=>'<th>'+h+'</th>').join('')+'</tr>\n</thead>\n<tbody>';
rows.forEach(function(s){
    var cc=s.change>0?'var(--green)':s.change<0?'var(--red)':'var(--text2)';
    var vc=s.vol_ratio>2?'var(--orange)':s.vol_ratio>1.5?'var(--gold)':'var(--text2)';
    var rc=s.rsi>70?'var(--red)':s.rsi<30?'var(--green)':'var(--text2)';
    var pinned=PINNED_SYMS.indexOf(s.symbol)>=0;
    var ac=pinned?'<span style="font-size:9px;color:var(--gold);font-family:Space Mono,monospace">PINNED</span>':'<button class="btn btn-gold" onclick="quickPinFromMovers(\''+s.symbol+'\',this)" style="font-size:10px;padding:3px 8px">Pin</button>';
    html+='<tr>'
        +'<td><span style="font-family:Space Mono,monospace;font-weight:700;font-size:12px">'+s.symbol+'</span></td>'
        +'<td style="font-family:Space Mono,monospace;font-size:11px">\u20b9'+s.ltp.toFixed(2)+'</td>'
        +'<td style="font-family:Space Mono,monospace;font-size:11px;color:'+cc+';font-weight:700">'+(s.change>=0?'+':'')+s.change.toFixed(2)+'%</td>'
        +'<td style="font-family:Space Mono,monospace;font-size:11px;color:'+vc+';font-weight:700">'+s.vol_ratio.toFixed(2)+'x</td>'
        +'<td style="font-family:Space Mono,monospace;font-size:11px;color:'+rc+'">'+s.rsi.toFixed(1)+'</td>'
        +'<td style="font-family:Space Mono,monospace;font-size:11px;color:var(--green-b)">\u20b9'+s.today_high.toFixed(2)+'</td>'
        +'<td style="font-family:Space Mono,monospace;font-size:11px;color:var(--red-b)">\u20b9'+s.today_low.toFixed(2)+'</td>'
        +'<td>'+ac+'</td>'
        +'</tr>';
});
html+='</tbody>\n</table>\n</div><div style="font-size:9px;color:var(--text3);margin-top:4px;font-family:Space Mono,monospace">'+rows.length+' stocks · as of '+(_moversData.as_of||'--')+'</div>';
mc.innerHTML=html;
}
function quickPinFromMovers(sym,btn){
btn.disabled=true;btn.innerHTML='...';
fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,action:'pin',direction:'BOTH'})})
.then(r=>r.json()).then(d=>{
    if(d.status==='ok'){PINNED_SYMS.push(sym);btn.outerHTML='<span style="font-size:9px;color:var(--gold)">PINNED</span>';showToast(sym+' pinned');loadPTData();}
    else{btn.disabled=false;btn.innerHTML='Pin';showToast(d.msg||'Error');}
}).catch(()=>{btn.disabled=false;btn.innerHTML='Pin';});
}
function _suggRow(s){
var rec=s.recommendation||s.direction||'NEUTRAL';
var rc=rec==='STRONG BUY'?'#00e676':rec==='BUY'?'var(--green)':rec==='STRONG SELL'?'#ff1744':rec==='SELL'?'var(--red)':'var(--text2)';
var rb=rec==='STRONG BUY'?'background:rgba(0,230,118,.07);':rec==='STRONG SELL'?'background:rgba(255,23,68,.07);':rec==='BUY'?'background:rgba(63,185,80,.04);':rec==='SELL'?'background:rgba(248,81,73,.04);':s.already_pinned?'background:rgba(240,192,64,.05)':'';
var cc=s.change>0?'var(--green)':s.change<0?'var(--red)':'var(--text2)';
var dir=s.direction||'BUY';
var scr5=dir==='BUY'?(s.buy5_score!=null?s.buy5_score:(s.buy_score!=null?s.buy_score:null)):(s.sell5_score!=null?s.sell5_score:(s.sell_score!=null?s.sell_score:null));
var scr15=dir==='BUY'?(s.buy15_score!=null?s.buy15_score:null):(s.sell15_score!=null?s.sell15_score:null);
var scr5txt=scr5!=null?scr5.toFixed(1):'—';
var scr15txt=scr15!=null?scr15.toFixed(1):'—';
var dirColor=dir==='BUY'?'var(--green)':'var(--red)';
var dirDimColor=dir==='BUY'?'rgba(63,185,80,.7)':'rgba(248,81,73,.7)';
var badges=(s.strategies||[]).slice(0,3).map(st=>'<span class="b '+(/BUY|BULL|BREAKOUT|MOMENTUM/i.test(st)?'bb':/SELL|BEAR|BREAKDOWN/i.test(st)?'bs':'bn')+'">'+st+'</span>').join('');
if((s.strategies||[]).length>3)badges+='<span class="b bn">+'+(s.strategies.length-3)+'</span>';
var dirAttr='data-dir="'+dir+'" data-rec="'+rec+'" data-score="'+s.composite_score+'"';
var ac=s.already_pinned
    ?'<button class="btn btn-gh" id="qpBtn_'+s.symbol+'" '+dirAttr+' style="padding:3px 8px;font-size:10px;width:100%;color:var(--gold);border-color:rgba(240,192,64,.3)" onclick="quickUnpin(\''+s.symbol+'\',this)"><i class="fas fa-thumbtack"></i> Unpin</button>'
    :'<button class="btn btn-gold" id="qpBtn_'+s.symbol+'" '+dirAttr+' style="padding:3px 8px;font-size:10px;width:100%" onclick="quickPin(\''+s.symbol+'\',this)"><i class="fas fa-thumbtack"></i> Pin</button>';
var mtfBadge=s.htf_align!=null
    ?(s.htf_align?'<span title="5-min and 15-min agree" style="font-size:14px">✅</span>':'<span title="Timeframes conflict" style="font-size:14px">⚠️</span>')
    :'<span style="color:var(--text3)">—</span>';
var rsiVal=(s.rsi!=null?s.rsi:(s.indicators?.rsi!=null?s.indicators.rsi:null));
var adxVal=(s.adx!=null?s.adx:(s.indicators?.adx!=null?s.indicators.adx:null));
var N='padding:7px 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
var M=N+'font-family:Space Mono,monospace;font-size:11px;';
return '<tr id="suggRow_'+s.symbol+'" style="border-bottom:1px solid rgba(48,54,61,.4);'+rb+'">'
    +'<td style="'+N+'"><span style="font-family:Space Mono,monospace;font-weight:700;font-size:12px">'+s.symbol+'</span></td>'
    +'<td style="'+M+'text-align:right">₹'+s.price.toFixed(2)+'</td>'
    +'<td style="'+M+'text-align:right;color:'+cc+'">'+(s.change>=0?'+':'')+s.change.toFixed(2)+'%</td>'
    +'<td style="'+M+'text-align:right;color:'+(s.gap_pct!=null?(s.gap_pct>0.8?'var(--green)':s.gap_pct<-0.8?'var(--red)':'var(--text3)'):'var(--text3)')+'">'+( s.gap_pct!=null?(s.gap_pct>=0?'+':'')+Number(s.gap_pct).toFixed(2)+'%':'--')+'</td>'
    +'<td style="'+M+'text-align:right;color:'+(s.vol_ratio!=null?(s.vol_ratio>2?'var(--orange)':s.vol_ratio>1.5?'var(--gold)':'var(--text2)'):'var(--text3)')+'">'+( s.vol_ratio!=null?Number(s.vol_ratio).toFixed(2)+'x':'--')+'</td>'
    +'<td style="'+N+'text-align:center"><span style="font-size:10px;font-weight:700;color:'+rc+'">'+rec+'</span></td>'
    +'<td style="'+M+'text-align:right;color:var(--blue);font-weight:700">'+s.composite_score+'</td>'
    +'<td style="'+M+'text-align:right;color:'+dirColor+'">'+scr5txt+'</td>'
    +'<td style="'+M+'text-align:right;color:'+dirDimColor+'">'+scr15txt+'</td>'
    +'<td style="'+N+'text-align:center">'+mtfBadge+'</td>'
    +'<td style="'+M+'text-align:right;color:var(--text2)">'+(rsiVal!=null?Number(rsiVal).toFixed(1):'—')+'</td>'
    +'<td style="'+M+'text-align:right;color:var(--text2)">'+(adxVal!=null?Number(adxVal).toFixed(1):'—')+'</td>'
    +'<td style="'+N+'padding-right:4px">'+badges+'</td>'
    +'<td style="'+N+'text-align:center">'+ac+'</td>'
    +'</tr>';
}

function _buildPinnedChips(pinned, meta){
meta=meta||ptData.pinned_meta||{};
if(!pinned.length)return '<div id="pinnedChipsWrap"><div class="es"><i class="fas fa-thumbtack"></i><p>No stocks monitored.<br><small>Use search above or Run Full Scan to add stocks</small></p></div></div>';
var html='<div id="pinnedChipsWrap"><div class="section-header"><span class="section-title">Currently Monitored (<span id="pinnedChipCount">'+pinned.length+'</span>)</span><div style="display:flex;align-items:center;gap:6px"><span style="font-size:10px;color:var(--text3)">Signal check every 1s</span><button class="btn btn-r" onclick="unpinAllMonitored(this)" style="font-size:10px;padding:3px 9px;height:24px" title="Remove all stocks from monitored list"><i class="fas fa-thumbtack" style="transform:rotate(45deg);display:inline-block"></i> Unpin All</button></div></div>'
    +'<div style="display:flex;flex-wrap:wrap;gap:8px;padding:4px 0;margin-bottom:14px">';
pinned.forEach(sym=>{
    var hasPos=ptData.positions&&ptData.positions[sym];
    var m=meta[sym]||{};
    var dir=m.direction||'BOTH';
    var rec=m.recommendation||'';
    var dirBadge='';
    if(dir==='BUY'||dir==='SELL'){
    var arrow=dir==='BUY'?'▲':'▼';
    var dirClr=dir==='BUY'?'#00e676':'#ff1744';
    var dirBg=dir==='BUY'?'rgba(0,230,118,.12)':'rgba(255,23,68,.12)';
    var recLabel=rec||dir;
    dirBadge='<span title="Pinned for '+recLabel+'" style="display:inline-flex;align-items:center;gap:2px;background:'+dirBg+';color:'+dirClr+';border:1px solid '+dirClr+';border-radius:4px;padding:1px 5px;font-size:9px;font-weight:700;font-family:Space Mono,monospace">'+arrow+' '+dir+'</span>';
    } else {
    dirBadge='<span title="Manual pin — trades both directions" style="color:var(--text3);font-size:9px;font-family:Space Mono,monospace">↕ BOTH</span>';
    }
    var borderClr=hasPos?(dir==='BUY'?'var(--green-b)':dir==='SELL'?'rgba(255,23,68,.5)':'var(--green-b)'):'var(--border)';
    html+='<div id="chip_'+sym+'" style="background:var(--bg1);border:1px solid '+borderClr+';border-radius:8px;padding:8px 12px;display:flex;align-items:center;gap:8px">'
    +'<span class="sym">'+sym+'</span>'
    +dirBadge
    +(hasPos?'<span class="b bb">OPEN</span>':'<span class="b bn">WATCHING</span>')
    +'<button class="pin-btn pinned" title="Unpin '+sym+'" onclick="unpinChip(\''+sym+'\',this)"><i class="fas fa-thumbtack"></i></button></div>';
});
html+='</div></div>';return html;
}
function _updatePinnedChips(pinned){
var cs=document.getElementById('pinnedCountStat');if(cs)cs.textContent=pinned.length;
var cc=document.getElementById('pinnedChipCount');if(cc)cc.textContent=pinned.length;
var w=document.getElementById('pinnedChipsWrap');
if(w)w.outerHTML=_buildPinnedChips(pinned, ptData.pinned_meta||{});
}

function unpinAllMonitored(btn){
var syms=PINNED_SYMS.slice();
if(!syms.length){showToast('No stocks monitored');return;}
if(!confirm('Remove all '+syms.length+' monitored stocks?'))return;
if(btn){btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';}
var done=0,failed=0;
function next(i){
    if(i>=syms.length){
        PINNED_SYMS=[];
        _updatePinnedChips([]);
        var tc=document.getElementById('pinnedCountStat');if(tc)tc.textContent=0;
        var tpc=document.getElementById('topPinnedCount');if(tpc)tpc.textContent=0;
        var spc=document.getElementById('sidebarPinCount');if(spc)spc.textContent=0;
        showToast('Unpinned '+done+' stocks'+(failed?' ('+failed+' failed)':''));
        loadPTData();
        return;
    }
    fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:syms[i],action:'unpin'})})
    .then(r=>r.json()).then(d=>{if(d.status==='ok')done++;else failed++;next(i+1);})
    .catch(()=>{failed++;next(i+1);});
}
next(0);
}

function unpinChip(sym,btn){
btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';
fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,action:'unpin'})})
.then(r=>r.json()).then(d=>{
    if(d.status==='ok'){
    PINNED_SYMS=PINNED_SYMS.filter(s=>s!==sym);
    var chip=document.getElementById('chip_'+sym);
    if(chip){chip.style.transition='opacity .3s';chip.style.opacity='0';setTimeout(()=>_updatePinnedChips(PINNED_SYMS.slice()),300);}
    var rb=document.getElementById('qpBtn_'+sym);
    if(rb&&rb.innerHTML.indexOf('Unpin')>-1){
        rb.outerHTML='<button class="btn btn-gold" id="qpBtn_'+sym+'" style="padding:3px 10px;font-size:10px" onclick="quickPin(\''+sym+'\',this)"><i class="fas fa-thumbtack"></i> Pin</button>';
        var row=document.getElementById('suggRow_'+sym);if(row)row.style.background='';
    }
    _refreshBulkCounts();_syncPinBadges();showToast('🗑 '+sym+' unpinned');
    }else{btn.disabled=false;btn.innerHTML='<i class="fas fa-thumbtack"></i>';showToast('❌ '+(d.msg||'Failed'));}
}).catch(()=>{btn.disabled=false;btn.innerHTML='<i class="fas fa-thumbtack"></i>';showToast('❌ Network error');});
}

function quickPin(sym,btn){
var orig=btn.innerHTML;btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';
var dir=btn.getAttribute('data-dir')||null;
var rec=btn.getAttribute('data-rec')||null;
var score=btn.getAttribute('data-score')?parseFloat(btn.getAttribute('data-score')):null;
fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym,action:'pin',direction:dir,recommendation:rec,score:score})})
.then(r=>r.json()).then(d=>{
    if(d.status==='ok'){
    if(PINNED_SYMS.indexOf(sym)<0)PINNED_SYMS.push(sym);
    btn.outerHTML='<button class="btn btn-gh" id="qpBtn_'+sym+'" style="padding:3px 10px;font-size:10px;color:var(--gold);border-color:rgba(240,192,64,.3)" onclick="quickUnpin(\''+sym+'\',this)"><i class="fas fa-thumbtack"></i> Unpin</button>';
    var row=document.getElementById('suggRow_'+sym);if(row)row.style.background='rgba(240,192,64,.05)';
    _refreshBulkCounts();_syncPinBadges();
    loadPTData();
    showToast('📌 '+sym+(dir?' → '+dir:'')+' pinned');
    }else{btn.innerHTML=orig;btn.disabled=false;showToast('❌ '+(d.msg||'Failed'));}
}).catch(()=>{btn.innerHTML=orig;btn.disabled=false;showToast('❌ Network error');});
}
function quickUnpin(sym,btn){
var orig=btn.innerHTML;btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';
fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym,action:'unpin'})})
.then(r=>r.json()).then(d=>{
    if(d.status==='ok'){
    PINNED_SYMS=PINNED_SYMS.filter(s=>s!==sym);
    btn.outerHTML='<button class="btn btn-gold" id="qpBtn_'+sym+'" style="padding:3px 10px;font-size:10px" onclick="quickPin(\''+sym+'\',this)"><i class="fas fa-thumbtack"></i> Pin</button>';
    var row=document.getElementById('suggRow_'+sym);if(row)row.style.background='';
    _refreshBulkCounts();_syncPinBadges();_updatePinnedChips(PINNED_SYMS.slice());showToast('🗑 '+sym+' unpinned');
    }else{btn.innerHTML=orig;btn.disabled=false;showToast('❌ '+(d.msg||'Failed'));}
}).catch(()=>{btn.innerHTML=orig;btn.disabled=false;showToast('❌ Network error');});
}
function pinAllSuggestions(){
var btn=document.getElementById('pinAllBtn');if(btn){btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i> Pinning...';}
var toPin=[];
document.querySelectorAll('[id^="qpBtn_"]').forEach(el=>{
    if(el.onclick&&el.onclick.toString().indexOf('quickPin')>-1){
    var sym=el.id.replace('qpBtn_','');
    if(sym&&PINNED_SYMS.indexOf(sym)<0){
        toPin.push({
        sym:  sym,
        dir:  el.getAttribute('data-dir')||null,
        rec:  el.getAttribute('data-rec')||null,
        score:el.getAttribute('data-score')?parseFloat(el.getAttribute('data-score')):null,
        });
    }
    }
});
if(!toPin.length){if(btn){btn.disabled=false;btn.innerHTML='<i class="fas fa-thumbtack"></i> Pin All';}showToast('ℹ️ All pinned');return;}
var idx=0,ok=[];
function next(){
    if(idx>=toPin.length){
    if(btn){btn.disabled=true;btn.innerHTML='<i class="fas fa-thumbtack"></i> Pin All (0)';}
    _syncPinBadges();_refreshBulkCounts();
    loadPTData();
    showToast('📌 Pinned '+ok.length);return;
    }
    var item=toPin[idx++];
    fetch('/paper/pin',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:item.sym,action:'pin',direction:item.dir,recommendation:item.rec,score:item.score})})
    .then(r=>r.json()).then(d=>{
    if(d.status==='ok'){
        if(PINNED_SYMS.indexOf(item.sym)<0)PINNED_SYMS.push(item.sym);ok.push(item.sym);
        var rb=document.getElementById('qpBtn_'+item.sym);
        if(rb)rb.outerHTML='<button class="btn btn-gh" id="qpBtn_'+item.sym+'" style="padding:3px 10px;font-size:10px;color:var(--gold);border-color:rgba(240,192,64,.3)" onclick="quickUnpin(\''+item.sym+'\',this)"><i class="fas fa-thumbtack"></i> Unpin</button>';
        var row=document.getElementById('suggRow_'+item.sym);if(row)row.style.background='rgba(240,192,64,.05)';
    }
    next();
    }).catch(()=>next());
}
next();
}
function unpinAllSuggestions(){
var btn=document.getElementById('unpinAllBtn');
if(btn){btn.disabled=true;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i> Unpinning...';}
var toUnpin=PINNED_SYMS.slice();
if(!toUnpin.length){
    if(btn){btn.disabled=false;btn.innerHTML='<i class="fas fa-thumbtack fa-flip-horizontal"></i> Unpin All';}
    showToast('ℹ️ Nothing to unpin');
    return;
}
var idx=0,ok=[];
function next(){
    if(idx>=toUnpin.length){
    if(btn){
        btn.disabled=true;
        btn.innerHTML='<i class="fas fa-thumbtack fa-flip-horizontal"></i> Unpin All (0)';
    }
    var pinBtn=document.getElementById('pinAllBtn');
    if(pinBtn){
        var uc=document.querySelectorAll('[id^="qpBtn_"]').length;
        pinBtn.disabled=(uc===0);
        pinBtn.innerHTML='<i class="fas fa-thumbtack"></i> Pin All'+(uc>0?' <span style="opacity:.7">('+uc+')</span>':'');
    }
    document.querySelectorAll('[id^="qpBtn_"]').forEach(el=>{
        var sym=el.id.replace('qpBtn_','');
        el.outerHTML='<button class="btn btn-gold" id="qpBtn_'+sym+'" style="padding:3px 10px;font-size:10px;width:100%" onclick="quickPin(\''+sym+'\',this)"><i class="fas fa-thumbtack"></i> Pin</button>';
        var row=document.getElementById('suggRow_'+sym);
        if(row)row.style.background='';
    });
    _syncPinBadges();
    _updatePinnedChips([]);
    showToast('🗑 Unpinned '+ok.length+' stock'+(ok.length!==1?'s':''));
    return;
    }
    var sym=toUnpin[idx++];
    fetch('/paper/pin',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:sym,action:'unpin'})
    })
    .then(r=>r.json())
    .then(d=>{
    if(d.status==='ok'){
        PINNED_SYMS=PINNED_SYMS.filter(s=>s!==sym);
        ok.push(sym);
    }
    next();
    })
    .catch(()=>next());
}
next();
}
function _refreshBulkCounts(){
var pBtn=document.getElementById('pinAllBtn'),uBtn=document.getElementById('unpinAllBtn');if(!pBtn||!uBtn)return;
var pc=0,uc=0;
document.querySelectorAll('[id^="qpBtn_"]').forEach(el=>{
    if(el.onclick&&el.onclick.toString().indexOf('quickUnpin')>-1)uc++;else pc++;
});
pBtn.disabled=(pc===0);uBtn.disabled=(uc===0);
pBtn.innerHTML='<i class="fas fa-thumbtack"></i> Pin All'+(pc>0?' <span style="opacity:.7">('+pc+')</span>':'');
uBtn.innerHTML='<i class="fas fa-thumbtack fa-flip-horizontal"></i> Unpin All'+(uc>0?' <span style="opacity:.7">('+uc+')</span>':'');
}
function _syncPinBadges(){
var e1=document.getElementById('sidebarPinCount'),e2=document.getElementById('topPinnedCount');
if(e1)e1.textContent=PINNED_SYMS.length;if(e2)e2.textContent=PINNED_SYMS.length;
}

function renderPTSigLog(el){
var first=!el.querySelector('#sigLogContainer');
if(first){el.innerHTML='<div class="es"><div class="spin"></div><p style="margin-top:10px">Loading...</p></div>';_sigLogPage=1;}
_fetchSigLog(el,first);
}

var _sigLogLastCount=-1;
var _sigLogPollingTimer=null;
function startSigLogPolling(){if(_sigLogPollingTimer)return;refreshSigLogBackground();_sigLogPollingTimer=setInterval(refreshSigLogBackground,4000);}
function stopSigLogPolling(){if(_sigLogPollingTimer){clearInterval(_sigLogPollingTimer);_sigLogPollingTimer=null;}}
function _fetchSigLog(el,reset){
if(_sigLogFetching)return;_sigLogFetching=true;
var today=new Date().toISOString().slice(0,10);
fetch('/paper/signal-logs?date='+today).then(r=>r.json()).then(d=>{
    _sigLogFetching=false;
    var newLogs=d.logs||[];
    var changed=newLogs.length!==_sigLogLastCount;
    _sigLogLastCount=newLogs.length;
    _sigLogAllLogs=newLogs;
    if(reset)_sigLogPage=1;
    var alive=document.getElementById('ptTabContent');
    if(alive&&(reset||changed)){
        var tw=alive.querySelector('.tw');
        var sx=tw?tw.scrollLeft:0,sy=tw?tw.scrollTop:0;
        _renderSigLogPage(alive);
        requestAnimationFrame(()=>{var tw2=alive.querySelector('.tw');if(tw2){tw2.scrollLeft=sx;tw2.scrollTop=sy;}});
    }
}).catch(()=>{
    _sigLogFetching=false;
    if(!el.querySelector('#sigLogContainer'))
    el.innerHTML='<div class="es" style="color:var(--orange)">⚠ Signal log unavailable.</div>';
});
}

function refreshSigLogBackground(){
if(ptTab!=='siglog')return;
var el=document.getElementById('ptTabContent');if(el)_fetchSigLog(el,false);
}

function _renderSigLogPage(el){
var logs=_sigLogAllLogs;
var cnts={
    'BUY_SIGNAL':0,'BUY_NO_FILL':0,
    'SELL_SIGNAL':0,'SELL_NO_FILL':0,
    'IN_POSITION':0,'REJECTED':0,'COOLDOWN':0,'ERROR':0
};
logs.forEach(l=>{
    if(cnts[l.status]!==undefined) cnts[l.status]++;
    else cnts['REJECTED']++;
});
if(_sigLogFilter!=='all'){
    if(_sigLogFilter==='BUY_SIGNAL')
    logs=logs.filter(l=>l.status==='BUY_SIGNAL'||l.status==='BUY_NO_FILL');
    else if(_sigLogFilter==='SELL_SIGNAL')
    logs=logs.filter(l=>l.status==='SELL_SIGNAL'||l.status==='SELL_NO_FILL');
    else if(_sigLogFilter==='IN_POSITION')
    logs=logs.filter(l=>l.status==='IN_POSITION');
    else if(_sigLogFilter==='REJECTED')
    logs=logs.filter(l=>['REJECTED','COOLDOWN','BLOCKED_OTHER_POS','ERROR'].includes(l.status));
}
var total=logs.length, tp=Math.ceil(total/_sigLogPP)||1;
if(_sigLogPage>tp)_sigLogPage=tp;
var start=(_sigLogPage-1)*_sigLogPP, pageLogs=logs.slice(start,start+_sigLogPP);
var buyTotal  = cnts['BUY_SIGNAL']  + cnts['BUY_NO_FILL'];
var sellTotal = cnts['SELL_SIGNAL'] + cnts['SELL_NO_FILL'];
var rejTotal  = cnts['REJECTED'] + cnts['COOLDOWN'] + cnts['ERROR'];
function _fBtn(filter, color, label, count, bg){
    var active=_sigLogFilter===filter;
    return `<button onclick="_sigLogSetFilter('${filter}')"
    style="padding:5px 10px;border-radius:20px;font-size:10px;font-weight:600;cursor:pointer;
            font-family:Space Mono,monospace;white-space:nowrap;
            border:1px solid ${active?color:'var(--border)'};
            background:${active?bg:'var(--bg2)'};
            color:${active?color:'var(--text1)'}"
    >${label} (${count})</button>`;
}
var ft='<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;background:var(--bg1);padding:10px;border-radius:8px;border:1px solid var(--border);">'
    +_fBtn('all',       'var(--blue)',  '📊 ALL',     logs.length,              'var(--accent)')
    +_fBtn('BUY_SIGNAL','#00e676',     '🟢 BUY',     buyTotal,                 'rgba(0,230,118,.2)')
    +_fBtn('SELL_SIGNAL','#ff1744',    '🔴 SELL',    sellTotal,                'rgba(255,23,68,.2)')
    +_fBtn('IN_POSITION','var(--gold)','🟡 IN POS',  cnts['IN_POSITION'],      'rgba(240,192,64,.2)')
    +_fBtn('REJECTED',  'var(--text1)','⚫ REJECTED', rejTotal,                'var(--bg3)')
    +'</div>';
if(total===0){
    el.innerHTML=ft+'<div style="background:var(--bg1);border:1px solid var(--border);border-radius:8px;padding:30px;text-align:center;color:var(--text3)">No signals match filter</div>';
    return;
}
function _statusBadge(status){
    switch(status){
    case 'BUY_SIGNAL':
        return '<span style="background:rgba(0,230,118,.15);color:#00e676;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid rgba(0,230,118,.3)">🟢 BUY</span>';
    case 'BUY_NO_FILL':
        return '<span style="background:rgba(0,230,118,.06);color:#00e676;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px dashed rgba(0,230,118,.35);opacity:.8">🟢 NO FILL</span>';
    case 'SELL_SIGNAL':
        return '<span style="background:rgba(255,23,68,.15);color:#ff1744;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid rgba(255,23,68,.3)">🔴 SELL</span>';
    case 'SELL_NO_FILL':
        return '<span style="background:rgba(255,23,68,.06);color:#ff1744;padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px dashed rgba(255,23,68,.35);opacity:.8">🔴 NO FILL</span>';
    case 'IN_POSITION':
        return '<span style="background:rgba(240,192,64,.15);color:var(--gold);padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid rgba(240,192,64,.3)">🟡 IN POS</span>';
    case 'COOLDOWN':
        return '<span style="background:rgba(139,148,158,.15);color:var(--text2);padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid var(--border)">⏳ COOLDOWN</span>';
    case 'BLOCKED_OTHER_POS':
        return '<span style="background:rgba(227,179,65,.12);color:var(--orange);padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid rgba(227,179,65,.3)">🔒 BLOCKED</span>';
    case 'ERROR':
        return '<span style="background:rgba(248,81,73,.12);color:var(--red);padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid rgba(248,81,73,.2)">❌ ERROR</span>';
    default:
        return '<span style="background:rgba(139,148,158,.1);color:var(--text3);padding:3px 8px;border-radius:4px;font-size:10px;font-weight:700;border:1px solid var(--border)">⚫ REJECTED</span>';
    }
}
function _rowBg(status){
    switch(status){
    case 'BUY_SIGNAL':    return 'background:rgba(0,230,118,.1);border-left:3px solid #00e676;';
    case 'BUY_NO_FILL':   return 'background:rgba(0,230,118,.04);border-left:3px dashed rgba(0,230,118,.4);';
    case 'SELL_SIGNAL':   return 'background:rgba(255,23,68,.1);border-left:3px solid #ff1744;';
    case 'SELL_NO_FILL':  return 'background:rgba(255,23,68,.04);border-left:3px dashed rgba(255,23,68,.4);';
    case 'IN_POSITION':   return 'background:rgba(240,192,64,.1);border-left:3px solid var(--gold);';
    case 'BLOCKED_OTHER_POS': return 'background:rgba(227,179,65,.06);border-left:3px solid rgba(227,179,65,.4);';
    case 'ERROR':         return 'background:rgba(248,81,73,.06);border-left:3px solid rgba(248,81,73,.3);';
    default: return '';
    }
}
function _reasonCell(l){
    var r=l.reason||'—';
    var isPostBlock = r.indexOf('POST-SIGNAL BLOCK')>=0 || r.indexOf('⚠')>=0;
    var isNoFill    = l.status==='BUY_NO_FILL'||l.status==='SELL_NO_FILL';
    var color = isPostBlock
    ? 'color:var(--orange)'
    : (isNoFill ? 'color:var(--orange);opacity:.8' : 'color:var(--text3)');
    return `<td style="max-width:240px;font-size:10px;${color};word-break:break-word;white-space:normal;line-height:1.4">${r}</td>`;
}
var rows='';
pageLogs.forEach(l=>{
    var bs=l.buy_score||0, ss=l.sell_score||0;
    var sb=l.soft_b||0,    ss2=l.soft_s||0;
    var vb=l.vote_b||0,    vs=l.vote_s||0;
    var stIcon=l.p6_buy?'▲':l.p6_sell?'▼':'—';
    var htf=l.htf_bull!=null?l.htf_bull:0.5;
    var stColor=stIcon==='▲'?'#00e676':stIcon==='▼'?'#ff1744':'var(--text2)';
    var htfColor=htf>=0.7?'#00e676':htf<=0.3?'#ff1744':'var(--text2)';
    var pd=l.pin_dir||'BOTH';
    var pdColor=pd==='BUY'?'#00e676':pd==='SELL'?'#ff1744':'var(--text3)';
    var pdBadge=`<span style="font-size:9px;font-family:Space Mono,monospace;color:${pdColor};border:1px solid ${pdColor};border-radius:3px;padding:1px 4px;opacity:.8">${pd}</span>`;
    rows+=`<tr style="${_rowBg(l.status)}">
    <td style="white-space:nowrap;font-size:10px;color:var(--text2)">${l.time||'--:--'}</td>
    <td style="min-width:90px">
        <span style="font-weight:700;color:var(--gold);font-family:Space Mono,monospace;font-size:12px">${l.symbol||'---'}</span><br>
        ${pdBadge}
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px">₹${Number(l.ltp||0).toFixed(2)}</td>
    <td style="font-family:Space Mono,monospace;font-size:11px">
        <span style="color:${bs>=70?'#00e676':'var(--text2)'};font-weight:${bs>=70?700:400}">${bs.toFixed(1)}</span>
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px">
        <span style="color:${ss>=70?'#ff1744':'var(--text2)'};font-weight:${ss>=70?700:400}">${ss.toFixed(1)}</span>
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px">
        <span style="color:${sb>=3?'#00e676':'var(--text2)'}">B:${sb}</span>
        <span style="color:${ss2>=3?'#ff1744':'var(--text2)'}"> S:${ss2}</span>
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px">
        <span style="color:${vb>=60?'#00e676':'var(--text2)'}">B:${vb.toFixed(0)}%</span>
        <span style="color:${vs>=60?'#ff1744':'var(--text2)'}"> S:${vs.toFixed(0)}%</span>
      </td>
    <td style="text-align:center;font-size:13px;color:${stColor}">${stIcon}</td>
    <td style="font-family:Space Mono,monospace;font-size:11px;color:${htfColor}">${htf.toFixed(2)}</td>
    <td>${_statusBadge(l.status)}</td>
    ${_reasonCell(l)}
     </tr>`;
});
var table=`
    <div style="background:var(--bg1);border:1px solid var(--border);border-radius:8px;overflow-x:auto">
    <table style="width:100%;border-collapse:collapse;min-width:900px">
        <thead>
        <tr style="background:var(--bg2);border-bottom:1px solid var(--border)">
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace;white-space:nowrap">TIME</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">SYMBOL</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">LTP</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--green);font-family:Space Mono,monospace">BUY SCR</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--red);font-family:Space Mono,monospace">SELL SCR</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">LAYERS</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">VOTE</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace;text-align:center">ST</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">HTF</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">STATUS</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">REASON</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
var pg='';
if(tp>1){
    pg='<div style="display:flex;justify-content:center;align-items:center;gap:5px;margin-top:15px;flex-wrap:wrap">';
    pg+=`<button style="background:var(--bg2);color:var(--text2);border:1px solid var(--border);border-radius:5px;padding:4px 9px;cursor:pointer;font-size:11px"
        onclick="_sigLogGoPage(${_sigLogPage-1})" ${_sigLogPage<=1?'disabled':''}>‹</button>`;
    for(var p=Math.max(1,_sigLogPage-2);p<=Math.min(tp,_sigLogPage+2);p++){
    var isActive=p===_sigLogPage;
    pg+=`<button style="background:${isActive?'var(--accent)':'var(--bg2)'};color:${isActive?'white':'var(--text2)'};border:1px solid ${isActive?'var(--accent)':'var(--border)'};border-radius:5px;padding:4px 9px;cursor:pointer;font-size:11px"
            onclick="_sigLogGoPage(${p})">${p}</button>`;
    }
    pg+=`<button style="background:var(--bg2);color:var(--text2);border:1px solid var(--border);border-radius:5px;padding:4px 9px;cursor:pointer;font-size:11px"
        onclick="_sigLogGoPage(${_sigLogPage+1})" ${_sigLogPage>=tp?'disabled':''}>›</button>`;
    pg+=`<span style="font-size:10px;color:var(--text3);font-family:Space Mono,monospace">${_sigLogPage}/${tp} · ${total} entries</span></div>`;
}
el.innerHTML=ft+table+pg;
}

function _sigLogSetFilter(filter){
_sigLogFilter=filter;_sigLogPage=1;
var el=document.getElementById('ptTabContent');if(el)_renderSigLogPage(el);
}
function _sigLogGoPage(page){
_sigLogPage=page;
var el=document.getElementById('ptTabContent');if(el)_renderSigLogPage(el);
}

function forceExit(sym){
if(!confirm('Force exit '+sym+' at market?'))return;
var btn=event.target;var orig=btn.innerHTML;btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';btn.disabled=true;
fetch('/paper/exit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({symbol:sym})})
.then(r=>r.json()).then(d=>{
    if(d.status==='ok'){showToast('✅ '+d.msg);setTimeout(()=>{loadPTData();setTimeout(loadPTData,1000);},100);}
    else{showToast('❌ '+(d.msg||'Error'));btn.innerHTML=orig;btn.disabled=false;}
}).catch(e=>{showToast('❌ '+e.message);btn.innerHTML=orig;btn.disabled=false;});
}
function saveWallet(){
var v=parseFloat(document.getElementById('walletInput').value);
if(isNaN(v)||v<=0){alert('Enter valid amount');return;}
var btn=document.querySelector('.wallet-edit .btn-gold');var orig=btn?btn.innerHTML:'';
if(btn){btn.innerHTML='<i class="fas fa-spinner fa-spin"></i>';btn.disabled=true;}
fetch('/paper/wallet',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({amount:v})})
.then(r=>r.json()).then(d=>{showToast('₹'+Number(v).toLocaleString('en-IN')+' wallet set ✓');loadPTData();})
.catch(()=>showToast('Error')).finally(()=>{if(btn){btn.innerHTML=orig;btn.disabled=false;}});
}

document.addEventListener('DOMContentLoaded',()=>{initPT();});
document.addEventListener('visibilitychange',()=>{
if(document.hidden){if(ptRefreshTimer){clearInterval(ptRefreshTimer);ptRefreshTimer=setInterval(loadPTData,PT_BG_REFRESH_INTERVAL);}}
else{if(ptRefreshTimer){clearInterval(ptRefreshTimer);ptRefreshTimer=setInterval(loadPTData,PT_REFRESH_INTERVAL);loadPTData();}}
});
</script>
</body>
</html>"""

# ==================== LOGIN TEMPLATE ====================
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Login</title>
</head>
<body style="background:#0d1117;color:#e6edf3;font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box;">
<div style="background:#161b22;padding:24px;border-radius:12px;border:1px solid #30363d;width:100%;max-width:340px;box-sizing:border-box;">
<h2 style="color:#f0c040;margin:0 0 16px;font-size:20px;">Alpha Scanner</h2>
{% if error %}
<div style="background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:#f85149;padding:9px 12px;border-radius:6px;font-size:12px;margin-bottom:14px;">{{ error }}</div>
{% endif %}
<form method="post">
<label style="display:block;margin-bottom:8px;font-size:13px;">Select User</label>
<select name="user_id" style="width:100%;padding:10px;background:#21262d;color:white;border:1px solid #30363d;border-radius:6px;font-size:14px;box-sizing:border-box;">
{% for id, data in users.items() %}
<option value="{{ id }}">{{ data.name }}</option>
{% endfor %}
</select>
<label style="display:block;margin:12px 0 8px;font-size:13px;">Password</label>
<input type="password" name="password" required autocomplete="current-password" style="width:100%;padding:10px;background:#21262d;color:white;border:1px solid #30363d;border-radius:6px;font-size:14px;box-sizing:border-box;">
<button type="submit" style="margin-top:14px;width:100%;padding:10px;background:#1f6feb;border:none;border-radius:6px;color:white;font-weight:bold;cursor:pointer;font-size:14px;">Login</button>
</form>
</div>
</body>
</html>
"""

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