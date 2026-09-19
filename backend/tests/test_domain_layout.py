"""Every package under `app/domains/` must be a real domain.

Deploy attribution treats a domain as an immediate subdirectory of `app/domains/`
holding an `entrypoint.py`. A package sitting there without one is labelled
`not-a-domain` and attributed to nothing, so a change to it resolves `domains=[]`
and ships no image even though the code reached a function. Shared glue belongs
under `app/common/`, where attribution reaches it through each entrypoint's import
closure. This test fails the moment such a package reappears here.
"""

from __future__ import annotations

from pathlib import Path

DOMAINS_DIR = Path(__file__).resolve().parents[1] / "app" / "domains"


def test_every_package_under_domains_has_an_entrypoint() -> None:
    """Each immediate subdirectory of `app/domains/` holds an `entrypoint.py`."""
    missing = sorted(
        path.name
        for path in DOMAINS_DIR.iterdir()
        if path.is_dir()
        and path.name != "__pycache__"
        and not path.name.startswith("__")
        and not (path / "entrypoint.py").is_file()
    )
    assert not missing, (
        f"{missing} sit under app/domains/ without an entrypoint.py, so deploy "
        "attribution drops their changes and ships no image. Move shared glue to "
        "app/common/ instead."
    )
