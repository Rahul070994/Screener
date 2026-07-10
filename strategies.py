# strategies.py — registry glue module
# ============================================================
# ultimate_scanner.py does:
#     import strategies
#     AVAILABLE_STRATEGIES = strategies.STRATEGY_REGISTRY
#
# AVAILABLE_STRATEGIES is expected to map a STRATEGY-SET NAME (what the
# user picks in Settings, e.g. "v4_high_trust") -> a dict of
# {individual_strategy_name: strategy_function}, i.e. one full
# strategy-file's `all_strategies` registry per key.
#
# PaperTradingEngine.PANEL_GATE_STRATEGY = 'v4_high_trust' — the
# panel/gate scoring system (_panel_gates, sector bias, HTF-conflict
# penalties, etc.) is only ever applied when the user has this exact
# key selected. Any other key added below gets purely native
# vote-based scoring from its own strategy functions (see
# PaperTradingEngine.panels_enabled() / _null_gates() in
# ultimate_scanner.py).
#
# To add another strategy module later:
#   1. Write it the same way as v4_high_trust.py (a module exposing
#      its own `all_strategies` dict of {name: func(df, ind) -> bool}).
#   2. Import it below and add one more entry to STRATEGY_REGISTRY,
#      e.g. STRATEGY_REGISTRY['my_new_strategy'] = my_module.all_strategies.
#   3. It will automatically show up in the Settings strategy dropdown
#      and run in native (non-panel) scoring mode — no other code
#      changes required.
# ============================================================

import v4_high_trust

STRATEGY_REGISTRY = {
    'v4_high_trust': v4_high_trust.all_strategies,
}