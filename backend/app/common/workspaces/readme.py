"""Finding the README a workspace's overview shows inside one config tarball.

Like HCP Terraform, the overview shows the README of the configuration the
workspace runs: the one in its working directory, else the one at the root of
the tarball. Both functions reach this module: the runs function reads the README
when it ingests a VCS upload, and the workspaces function reads it the first time
an API uploaded config version is shown. Either way the result is stored on the
config version row, so a page view never touches the tarball or GitHub.

Everything is bounded: the archive is streamed rather than downloaded, a README
over `MAX_SCANNED_README_BYTES` is ignored, and the stored text is cut at
`MAX_README_BYTES` so the row stays well inside DynamoDB's item limit.
"""

from __future__ import annotations

import posixpath
import tarfile
from typing import IO, Any, Final, Optional

from ..composition.settings import Settings

README_NAMES: Final = ("readme.md", "readme.markdown", "readme")
"""Accepted file names, lower cased, most preferred first."""

MAX_README_BYTES: Final = 64_000
"""The most README text a config version row stores."""

MAX_SCANNED_README_BYTES: Final = 5_000_000
"""A README member larger than this is skipped rather than read."""


def _directory(working_directory: str) -> str:
    """The working directory as a tar member prefix: no leading `./`, no slashes at either end."""
    return working_directory.strip().removeprefix("./").strip("/")


def _rank(filename: str) -> Optional[int]:
    """Where a file name sits in `README_NAMES`, or `None` when it is not a README."""
    lowered = filename.lower()
    return README_NAMES.index(lowered) if lowered in README_NAMES else None


def _decode(raw: bytes) -> tuple[str, bool]:
    """The README text cut to `MAX_README_BYTES` on a line break, and whether it was cut."""
    if len(raw) <= MAX_README_BYTES:
        return raw.decode("utf-8", errors="replace"), False
    cut = raw[:MAX_README_BYTES]
    newline = cut.rfind(b"\n")
    if newline > 0:
        cut = cut[: newline + 1]
    return cut.decode("utf-8", errors="ignore"), True


def find_readme(fileobj: IO[bytes], working_directory: str = "") -> Optional[dict[str, Any]]:
    """The README a gzipped config tarball holds for this working directory.

    Returns `{"path", "content", "truncated"}` or `None`. The working directory's
    README wins over the root's, and within one directory `README.md` wins over
    the other accepted names. A broken archive reads as no README.
    """
    directory = _directory(working_directory)
    wanted = [directory, ""] if directory else [""]
    best: Optional[tuple[int, int, str, bytes]] = None
    try:
        with tarfile.open(fileobj=fileobj, mode="r|gz") as archive:
            for member in archive:
                if not member.isfile() or member.size > MAX_SCANNED_README_BYTES:
                    continue
                name = member.name.removeprefix("./")
                parent, filename = posixpath.split(name)
                if parent not in wanted:
                    continue
                rank = _rank(filename)
                if rank is None:
                    continue
                place = wanted.index(parent)
                if best is not None and (place, rank) >= (best[0], best[1]):
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                best = (place, rank, name, handle.read())
                if place == 0 and rank == 0:
                    break
    except (tarfile.TarError, EOFError, OSError):
        return None
    if best is None:
        return None
    content, truncated = _decode(best[3])
    return {"path": best[2], "content": content, "truncated": truncated}


def readme_fields(readme: Optional[dict[str, Any]]) -> dict[str, Any]:
    """The config version row attributes one scan result is stored as.

    `readme_scanned` is written either way, so a tarball with no README is not
    streamed again on the next view.
    """
    fields: dict[str, Any] = {"readme_scanned": True}
    if readme is not None:
        fields["readme"] = {
            "path": str(readme["path"]),
            "content": str(readme["content"]),
            "truncated": bool(readme["truncated"]),
        }
    return fields


def read_config_readme(
    bucket: str,
    key: str,
    working_directory: str,
    *,
    settings: Settings,
) -> Optional[dict[str, Any]]:
    """The README inside the tarball at `key`, streamed from the bucket.

    Raises botocore's `ClientError` when the object cannot be read, so a caller can
    tell an expired tarball from one with no README.
    """
    import boto3

    client: Any = boto3.client(
        "s3",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.s3_endpoint_url,
    )
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        return find_readme(body, working_directory)
    finally:
        body.close()
