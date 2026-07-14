# ============================================================
# modules/templates.py
# ------------------------------------------------------------
# HTML template constants (HTML, LOGIN_HTML) extracted out of
# ultimate_scanner.py as a pure code-organization refactor.
# NO BEHAVIOR CHANGE — these are the exact same strings that used
# to live inline in ultimate_scanner.py; only their location moved.
#
# These are plain string constants (Flask/Jinja templates), so
# there is no circular-import concern here (unlike modules/scanner.py):
# ultimate_scanner.py just does
#     from modules.templates import HTML, LOGIN_HTML
# near its other module imports.
# ============================================================

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
    <span style="color:var(--text3);font-size:9px">UNIVERSE</span> <span style="color:var(--gold)" id="topUniverseCount">{{ universe_count }} NIFTY 200</span>
    </div>
</div>
<div class="tc-time" id="clk"></div>
</div>
<div class="shell">
<nav class="sidebar" id="sidebar">
<div class="ns">Algo Trading</div>
<div class="ni active">
    <i class="fas fa-robot"></i> Paper Trading
    <span class="pin-count" id="sidebarUniverseCount">{{ universe_count }}</span>
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

function initPT(){
loadPTData();
startPTRefresh(PT_REFRESH_INTERVAL);
startMarketClock();
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
    +'<div class=\"sc2\"><div class=\"v\" style=\"color:var(--gold)\">'+(d.universe_count||200)+'</div><div class=\"l\">NIFTY 200</div></div>'
    +'</div>';
var el=document.getElementById('ptCards');if(el)el.innerHTML=html;
_updateBannerLive(d);
}

function _modeWindowTxt(mode){
return mode==='DELIVERY' ? 'No forced square-off · Entries allowed 9:30–15:30' : 'SqOff 15:15 · Trading Window: 9:15–14:30 ⭐⭐⭐⭐⭐';
}
function _updateBannerLive(d){
var mode = d.mode || CURRENT_MODE || 'INTRADAY';
var modeLabel = mode==='DELIVERY' ? 'CNC/Delivery' : 'Intraday';
var tgt = d.target_pct!=null ? d.target_pct : 0;
var sl = d.sl_pct!=null ? d.sl_pct : 0;
var line=document.getElementById('bannerModeLine');
if(line){
    line.innerHTML = '80% wallet · Margin-based sizing · [' + modeLabel + '] Target +' + tgt.toFixed(2)
        + '% · SL -' + sl.toFixed(2) + '% (Settings → Target &amp; Stop Loss) · ' + _modeWindowTxt(mode)
        + ' · Score≥35 · Vote≥50% · Vol≥1.3×';
}
var universeB=document.getElementById('bannerUniverseBadge');
if(universeB) universeB.innerHTML='<i class="fas fa-chart-line"></i> '+(d.universe_count||200)+' NIFTY 200';
var openB=document.getElementById('bannerOpenBadge');
if(openB){ openB.textContent=(d.open_positions||0)+' Open'; openB.className='b '+((d.open_positions||0)>0?'bb':'bn'); }
var winB=document.getElementById('bannerWinBadge');
if(winB) winB.textContent=(d.win_trades||0)+' ✅';
var lossB=document.getElementById('bannerLossBadge');
if(lossB) lossB.textContent=(d.loss_trades||0)+' ❌';
var wrB=document.getElementById('bannerWinRateBadge');
if(wrB) wrB.textContent='Win Rate: '+(d.win_rate||0)+'%';
CURRENT_MODE = mode;
if(d.strategy_name) CURRENT_STRATEGY = d.strategy_name;
}

function ptSwitchTab(tab){
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
            loadPTData();
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
        loadPTData();
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
        CURRENT_STRATEGY = strategy;
        loadPTData();
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
            <p style="color:var(--text3);font-size:12px;margin-bottom:16px;">Test your selected strategy against all NIFTY 200 stocks over a custom date range.</p>
            <div style="background:var(--bg1);border:1px solid var(--border);border-radius:10px;padding:16px;">
                <div class="rgrid4">
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
                <div id="backtestStats" class="rgrid4" style="margin-bottom:16px;"></div>
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
    el.innerHTML='<div class=\"es\"><i class=\"fas fa-robot\"></i><p>No open positions.<br><small>Monitoring <b>NIFTY 200</b> stocks \u00b7 Target <span style=\"color:var(--green)\">+'+tp.toFixed(1)+'%</span> \u00b7 SL <span style=\"color:var(--red)\">-'+sl.toFixed(1)+'%</span><br><span style=\"color:var(--red);font-size:10px\">P&L shown NET of charges</span></small></p></div>';
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
function _strategyDataCell(l){
    var sd = l.strategy_data || {};
    var names = Object.keys(sd);
    if(!names.length) return '<td style="font-size:10px;color:var(--text3)">—</td>';
    var html = names.map(function(name){
        var fields = sd[name] || {};
        var parts = Object.keys(fields).map(function(k){
            return '<span style="color:var(--text3)">'+k+':</span> <span style="color:var(--text0)">'+fields[k]+'</span>';
        }).join('&nbsp;&nbsp;');
        return '<div style="margin-bottom:2px;white-space:nowrap"><span style="color:var(--gold);font-weight:700">'+name+'</span>&nbsp;&nbsp;'+parts+'</div>';
    }).join('');
    return '<td style="font-family:Space Mono,monospace;font-size:10px;line-height:1.5;white-space:normal;min-width:200px">'+html+'</td>';
}
var rows='';
pageLogs.forEach(l=>{
    var bs=l.buy_score||0, ss=l.sell_score||0;
    var htf=l.htf_bull!=null?l.htf_bull:0.5;
    var htfColor=htf>=0.7?'#00e676':htf<=0.3?'#ff1744':'var(--text2)';
    rows+=`<tr style="${_rowBg(l.status)}">
    <td style="white-space:nowrap;font-size:10px;color:var(--text2)">${l.time||'--:--'}</td>
    <td style="min-width:90px">
        <span style="font-weight:700;color:var(--gold);font-family:Space Mono,monospace;font-size:12px">${l.symbol||'---'}</span>
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px">₹${Number(l.ltp||0).toFixed(2)}</td>
    <td style="font-family:Space Mono,monospace;font-size:11px">
        <span style="color:${bs>=70?'#00e676':'var(--text2)'};font-weight:${bs>=70?700:400}">${bs.toFixed(1)}</span>
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px">
        <span style="color:${ss>=70?'#ff1744':'var(--text2)'};font-weight:${ss>=70?700:400}">${ss.toFixed(1)}</span>
      </td>
    <td style="font-family:Space Mono,monospace;font-size:11px;color:${htfColor}">${htf.toFixed(2)}</td>
    ${_strategyDataCell(l)}
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
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace">HTF</th>
            <th style="padding:9px 8px;font-size:9px;color:var(--text3);font-family:Space Mono,monospace;min-width:200px">STRATEGY DATA</th>
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