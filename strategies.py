import os
import sys
import importlib
import importlib.util
import pkgutil

_current_dir = os.path.dirname(os.path.abspath(__file__))
_strategies_subfolder = os.path.join(_current_dir, 'strategies')

if os.path.isdir(_strategies_subfolder) and _strategies_subfolder not in sys.path:
    sys.path.insert(0, _strategies_subfolder)
    print(f"Added strategies subfolder to sys.path: {_strategies_subfolder}")

# Files that live next to ultimate_scanner.py but are NOT strategy modules
# and must never be auto-imported by the scan below. Importing a .py file
# runs all of its top-level code — encrypt_keys.py and set_password.py are
# one-off provisioning/admin scripts that write to users.json as a side
# effect of being imported, which previously caused users.json to be
# silently overwritten (wiping password_hash) every time ultimate_scanner.py
# started, since they sat in this same folder and got swept up here.
_NON_STRATEGY_FILES = {
    'ultimate_scanner', 'strategies', 'app', 'main', 'wsgi', 'config',
    'setup', '__init__',
    'encrypt_keys', 'set_password',
}

STRATEGY_REGISTRY = {}
STRATEGY_META = {}
# Optional per-strategy diagnostics functions: {strategy_name: fn(df, ind) -> dict}.
# A strategy module MAY define a module-level `strategy_diagnostics` dict
# (same shape/pattern as `strategy_meta` above) mapping its function names to
# a diagnostics callable that returns a small {label: value} dict describing
# the strategy's own decision variables for the current bar (e.g. EMA20,
# EMA50, Diff% for an EMA-crossover strategy). This is entirely optional and
# additive — a strategy that doesn't define it just contributes nothing here,
# and the Signal Log UI simply shows nothing extra for that strategy. This is
# what lets the live Signal Log show "whatever the strategy actually computed"
# dynamically per-strategy, without ultimate_scanner.py hardcoding any
# knowledge of individual strategies' internals.
# Optional per-strategy early-exit functions: {strategy_name: fn(df, ind, pos) -> bool}.
# A strategy module MAY define a module-level `strategy_exits` dict (same
# shape/pattern as `strategy_diagnostics` above) mapping its function names
# to a reversal-exit callable that decides whether an OPEN position should
# be closed immediately (ahead of target/SL) because its own setup has
# invalidated. This is entirely optional and additive — a strategy that
# doesn't define it just contributes nothing here, and the position monitor
# simply never calls anything for that strategy. This is what lets
# ultimate_scanner.py's AVAILABLE_STRATEGY_EXITS pick up every strategy's
# reversal-exit function without ever needing to know that any particular
# strategy (e.g. EMA20/EMA50 flip) exists.
STRATEGY_EXITS = {}
STRATEGY_DIAGNOSTICS = {}
_STRATEGY_SOURCE_MODULE = {}
# Tracks, per MODULE name, which priority level (and file path) actually won
# registration — see _register_module's docstring for why this exists.
_MODULE_PRIORITY = {}
_MODULE_SOURCE_PATH = {}

def _load_module_from_file(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    # CRITICAL: module_from_spec()+exec_module() does NOT register the module
    # in sys.modules the way importlib.import_module() does. Without this
    # line, ultimate_scanner.py's _get_strategy_min_bars() — which looks a
    # strategy function's module up via sys.modules.get(fn.__module__) — can
    # never find modules loaded from this function (i.e. every strategy file
    # sitting directly next to ultimate_scanner.py, as opposed to inside the
    # strategies/ subfolder, which goes through import_module() and got this
    # registration for free). That silently broke each such module's
    # MIN_BARS_REQUIRED override, falling back to the 160-bar default even
    # for strategies (like ORB) that declare a much smaller requirement —
    # which in turn made single/short-day backtests skip every candidate bar
    # for that strategy (since a session only has ~75 5-min bars).
    sys.modules[module_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        # Don't leave a half-initialized module registered under this name
        # if exec_module blew up partway through.
        sys.modules.pop(module_name, None)
        raise
    return mod

def _validate_module(name, mod):
    all_strats = getattr(mod, 'all_strategies', None)
    if not isinstance(all_strats, dict) or not all_strats:
        return None
    bad = [k for k, v in all_strats.items() if not callable(v)]
    if bad:
        print(f"✗ Skipping strategy module '{name}': non-callable entries {bad}")
        return None
    return all_strats

def _register_module(name, mod, file_path=None, priority=0):
    """Register a strategy module's contents.

    priority: higher wins on a module-NAME collision (not a strategy-name
    collision — that's handled separately below via _STRATEGY_SOURCE_MODULE).
    Root-level files (next to ultimate_scanner.py — priority=1) always beat
    a same-named file sitting in the strategies/ subfolder (priority=0),
    regardless of scan order, because the root level is where this project's
    actively-edited strategy files live (per intraday_strategy.py's own
    location). Previously this function did:
        if name in STRATEGY_REGISTRY: return True
    which silently kept whichever module happened to be scanned FIRST
    (the strategies/ subfolder, scanned before the root dir) and threw away
    the root-level file with zero warning — meaning edits to e.g.
    intraday_strategy.py at the repo root could be completely invisible to
    the running system forever if a stale/duplicate copy with the same
    module name existed in strategies/. Now: any name collision is loud
    (prints both file paths + last-modified times), and the higher-priority
    (root-level) copy always wins the STRATEGY_REGISTRY/META/etc. entries,
    so "which file is actually live" is both deterministic and visible in
    the startup log instead of a silent no-op.
    """
    existing_priority = _MODULE_PRIORITY.get(name)
    if existing_priority is not None and existing_priority >= priority:
        existing_path = _MODULE_SOURCE_PATH.get(name, '?')
        print(
            f"⚠ Module name collision: '{name}' already registered from "
            f"'{existing_path}' — skipping lower/equal-priority copy at "
            f"'{file_path or '?'}' (this file's changes will NOT take effect "
            f"until the collision is resolved, e.g. rename/remove one copy)"
        )
        return True
    all_strats = _validate_module(name, mod)
    if all_strats is None:
        return False
    if existing_priority is not None:
        existing_path = _MODULE_SOURCE_PATH.get(name, '?')
        try:
            existing_mtime = os.path.getmtime(existing_path) if existing_path != '?' else None
            new_mtime = os.path.getmtime(file_path) if file_path else None
        except OSError:
            existing_mtime = new_mtime = None
        print(
            f"⚠ Module name collision: '{name}' registered from both "
            f"'{existing_path}' (mtime={existing_mtime}) and "
            f"'{file_path or '?'}' (mtime={new_mtime}) — using the "
            f"higher-priority copy at '{file_path or '?'}'"
        )
    _MODULE_PRIORITY[name] = priority
    _MODULE_SOURCE_PATH[name] = file_path or getattr(mod, '__file__', '?')
    meta = getattr(mod, 'strategy_meta', {}) or {}
    diagnostics = getattr(mod, 'strategy_diagnostics', {}) or {}
    exits = getattr(mod, 'strategy_exits', {}) or {}
    STRATEGY_REGISTRY[name] = all_strats
    for strat_name, fn in all_strats.items():
        if strat_name in _STRATEGY_SOURCE_MODULE and _STRATEGY_SOURCE_MODULE[strat_name] != name:
            print(f"⚠ Strategy name collision: '{strat_name}' defined in both "
                  f"'{_STRATEGY_SOURCE_MODULE[strat_name]}' and '{name}' — keeping '{name}' version")
        _STRATEGY_SOURCE_MODULE[strat_name] = name
        STRATEGY_META[strat_name] = meta.get(strat_name, STRATEGY_META.get(strat_name, {}))
        diag_fn = diagnostics.get(strat_name)
        if diag_fn is not None:
            if not callable(diag_fn):
                print(f"⚠ Skipping strategy_diagnostics['{strat_name}'] in '{name}': not callable")
            else:
                STRATEGY_DIAGNOSTICS[strat_name] = diag_fn
        exit_fn = exits.get(strat_name)
        if exit_fn is not None:
            if not callable(exit_fn):
                print(f"⚠ Skipping strategy_exits['{strat_name}'] in '{name}': not callable")
            else:
                STRATEGY_EXITS[strat_name] = exit_fn
    print(f"✓ {name} imported successfully ({len(all_strats)} strategies)")
    return True

def _scan_directory(directory, use_import_module, priority):
    if not directory or not os.path.isdir(directory):
        return
    for finder, mod_name, is_pkg in pkgutil.iter_modules([directory]):
        if is_pkg or mod_name.startswith('_') or mod_name in _NON_STRATEGY_FILES:
            continue
        file_path = os.path.join(directory, f"{mod_name}.py")
        try:
            if use_import_module:
                mod = importlib.import_module(mod_name)
                file_path = getattr(mod, '__file__', file_path)
            else:
                mod = _load_module_from_file(mod_name, file_path)
        except Exception as e:
            print(f"✗ Failed to import candidate strategy module '{mod_name}': {e}")
            continue
        _register_module(mod_name, mod, file_path=file_path, priority=priority)

# Root-level files (next to ultimate_scanner.py, priority=1) always win a
# module-name collision over the strategies/ subfolder (priority=0) — see
# _register_module. Scan order no longer determines the winner, only the
# priority does, so this could be called in either order safely.
_scan_directory(_strategies_subfolder, use_import_module=True, priority=0)
_scan_directory(_current_dir, use_import_module=False, priority=1)

print(f"Loaded strategy sets: {list(STRATEGY_REGISTRY.keys())}")
print(f"Total individual strategies: {len(STRATEGY_META)}")

if not STRATEGY_REGISTRY:
    raise RuntimeError(
        "No strategy modules could be imported. Add a .py file (either next to "
        "ultimate_scanner.py or inside a 'strategies' subfolder) that defines "
        "an 'all_strategies' dict of {name: fn(df, ind) -> bool}."
    )