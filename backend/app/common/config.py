"""The application's settings, re-exported from the composition package.

The import path the rest of the application uses, so a domain module need not
know which composition root built the settings it reads.
"""

from .composition.settings import Settings, get_settings, reset_settings_cache

__all__ = ["Settings", "get_settings", "reset_settings_cache"]
