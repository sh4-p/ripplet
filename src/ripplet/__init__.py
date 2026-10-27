"""Ripplet - evidence-driven project intelligence engine."""
__version__ = "0.1.3.4"
__app_name__ = "ripplet"

from .legacy import adopt_legacy_env as _adopt_legacy_env

_adopt_legacy_env()
