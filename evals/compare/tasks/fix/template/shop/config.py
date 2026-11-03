"""Settings with defaults and environment overrides."""

import os

DEFAULTS = {
    "currency_symbol": "",
    "low_stock_threshold": 5,
    "handling_days": 2,
    "return_window_days": 10,
    "report_top_n": 3,
}


def load(environ=None, overrides=None):
    """Merge defaults, SHOP_* environment variables and explicit overrides (last wins)."""
    environ = os.environ if environ is None else environ
    settings = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        raw = environ.get("SHOP_" + key.upper())
        if raw is None:
            continue
        settings[key] = int(raw) if isinstance(default, int) else raw
    settings.update(overrides or {})
    return settings


def describe(settings):
    return "\n".join("%s=%s" % (k, settings[k]) for k in sorted(settings))
