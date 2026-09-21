"""The shared suite's seven groups, collected against this deployment.

The star import is the whole point: every case lives in `webbpulse.e2e.suite` and is
collected here so a group added upstream reaches this product without an edit.

Pyright refuses a wildcard import from a library on principle, but that is exactly
what is wanted here and what the package documents, so the rule is suppressed on this
one line rather than for the file or the project.
"""

from __future__ import annotations

from webbpulse.e2e.suite import *  # noqa: F401,F403  # pyright: ignore[reportWildcardImportFromLibrary]
