"""Presigned GETs for artifacts already in S3.

`webbpulse.storage` mints presigned PUTs but has no GET equivalent, and the run
bundle needs one for the config tarball and the binary plan. This reuses the
library's cached, SigV4-pinned client rather than building a second one, so a
test that resets `webbpulse.storage.reset_client_cache` resets this too. When the
library grows a `presigned_get` this module becomes a one line forward.
"""

from __future__ import annotations

from typing import Final

from webbpulse.storage import DEFAULT_EXPIRES_IN, MAX_EXPIRES_IN, S3Presigner

DEFAULT_DOWNLOAD_EXPIRES_IN: Final = DEFAULT_EXPIRES_IN
"""Fifteen minutes, matching the upload side."""


def presigned_get(
    bucket: str,
    key: str,
    expires_in: int = DEFAULT_DOWNLOAD_EXPIRES_IN,
    *,
    client: S3Presigner | None = None,
    region_name: str | None = None,
    endpoint_url: str | None = None,
) -> str:
    """Mint a presigned `GET` for one object.

    Args:
        bucket: The bucket holding the object.
        key: The object key the URL authorises, and only that key.
        expires_in: Seconds the URL stays valid, at most `MAX_EXPIRES_IN`.
        client: An S3 client or a fake. Defaults to the library's cached client.
        region_name: Region for the default client.
        endpoint_url: Endpoint for the default client, for a local S3 stand-in.

    Raises:
        ValueError: When `bucket` or `key` is empty, or `expires_in` is outside 1
            to `MAX_EXPIRES_IN`, each of which mints a URL S3 rejects.
    """
    if not bucket:
        raise ValueError("presigned_get needs a bucket name.")
    if not key:
        raise ValueError("presigned_get needs an object key.")
    if not 1 <= expires_in <= MAX_EXPIRES_IN:
        raise ValueError(f"expires_in must be between 1 and {MAX_EXPIRES_IN} seconds, got {expires_in}.")

    from webbpulse.storage import _client as cached_client  # pyright: ignore[reportPrivateUsage]

    presigner = client if client is not None else cached_client(region_name, endpoint_url)
    return presigner.generate_presigned_url(
        ClientMethod="get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
        HttpMethod="GET",
    )


__all__ = ["DEFAULT_DOWNLOAD_EXPIRES_IN", "presigned_get"]
