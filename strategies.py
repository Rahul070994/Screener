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
_STRATEGY_SOURCE_MODULE = {}

def _load_module_from_file(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
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

def _register_module(name, mod):
    if name in STRATEGY_REGISTRY:
        return True
    all_strats = _validate_module(name, mod)
    if all_strats is None:
        return False
    meta = getattr(mod, 'strategy_meta', {}) or {}
    STRATEGY_REGISTRY[name] = all_strats
    for strat_name, fn in all_strats.items():
        if strat_name in _STRATEGY_SOURCE_MODULE and _STRATEGY_SOURCE_MODULE[strat_name] != name:
            print(f"⚠ Strategy name collision: '{strat_name}' defined in both "
                  f"'{_STRATEGY_SOURCE_MODULE[strat_name]}' and '{name}' — keeping '{name}' version")
        _STRATEGY_SOURCE_MODULE[strat_name] = name
        STRATEGY_META[strat_name] = meta.get(strat_name, STRATEGY_META.get(strat_name, {}))
    print(f"✓ {name} imported successfully ({len(all_strats)} strategies)")
    return True

def _scan_directory(directory, use_import_module):
    if not directory or not os.path.isdir(directory):
        return
    for finder, mod_name, is_pkg in pkgutil.iter_modules([directory]):
        if is_pkg or mod_name.startswith('_') or mod_name in _NON_STRATEGY_FILES:
            continue
        try:
            if use_import_module:
                mod = importlib.import_module(mod_name)
            else:
                file_path = os.path.join(directory, f"{mod_name}.py")
                mod = _load_module_from_file(mod_name, file_path)
        except Exception as e:
            print(f"✗ Failed to import candidate strategy module '{mod_name}': {e}")
            continue
        _register_module(mod_name, mod)

_scan_directory(_strategies_subfolder, use_import_module=True)
_scan_directory(_current_dir, use_import_module=False)

print(f"Loaded strategy sets: {list(STRATEGY_REGISTRY.keys())}")
print(f"Total individual strategies: {len(STRATEGY_META)}")

if not STRATEGY_REGISTRY:
    raise RuntimeError(
        "No strategy modules could be imported. Add a .py file (either next to "
        "ultimate_scanner.py or inside a 'strategies' subfolder) that defines "
        "an 'all_strategies' dict of {name: fn(df, ind) -> bool}."
    )