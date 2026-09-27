"""The Terraform module registry protocol, as `terraform init` calls it.

Mounted at the root because service discovery points `modules.v1` at
`<api host>/v1/modules/`. Terraform sends the `TF_TOKEN_<host>` credential for
the module source's host as a bearer, which is a `wpk_` key holding
`registry:read`. The routes are exposed past the staging gate with
`authorization_type = "NONE"`, so no authorizer context exists and only a key
verified here in process gets through.

Left out of the OpenAPI document, since the protocol is Terraform's contract
rather than this API's.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from ...common.core.auth import REGISTRY_READ, scopes
from . import service

router = APIRouter(
    prefix="/v1/modules",
    include_in_schema=False,
    dependencies=[Depends(scopes(REGISTRY_READ))],
)

Segment = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z_-]+$")]
Version = Annotated[str, Path(min_length=5, max_length=128, pattern=r"^[0-9A-Za-z.+-]+$")]


def _not_found(error: Exception) -> HTTPException:
    """The 404 Terraform reports as a module or version that does not exist."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"message": f"{error} was not found.", "error_code": "REGISTRY_NOT_FOUND"},
    )


@router.get("/{namespace}/{name}/{provider}/versions")
def list_versions(namespace: Segment, name: Segment, provider: Segment) -> dict[str, Any]:
    """The module's published versions, in the protocol's response shape."""
    try:
        versions = service.published_versions(namespace, name, provider)
    except service.ModuleNotFound as error:
        raise _not_found(error) from error
    return {"modules": [{"versions": [{"version": version} for version in versions]}]}


@router.get("/{namespace}/{name}/{provider}/{version}/download", status_code=status.HTTP_204_NO_CONTENT)
def download(
    namespace: Segment,
    name: Segment,
    provider: Segment,
    version: Version,
) -> Response:
    """A `204` whose `X-Terraform-Get` is a short lived presigned URL for the tarball."""
    try:
        url = service.download_url(namespace, name, provider, version)
    except service.ModuleNotFound as error:
        raise _not_found(error) from error
    return Response(
        status_code=status.HTTP_204_NO_CONTENT,
        headers={"X-Terraform-Get": url, "Cache-Control": "no-store"},
    )


__all__ = ["router"]
