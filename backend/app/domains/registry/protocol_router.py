"""The Terraform module and provider registry protocols, as `terraform init` calls them.

Mounted at the root because service discovery points `modules.v1` at
`<api host>/v1/modules/` and `providers.v1` at `<api host>/v1/providers/`.
Terraform sends the `TF_TOKEN_<host>` credential for the source's host as a bearer, which is a `wpk_` key holding
`registry:read`. The routes are exposed past the staging gate with
`authorization_type = "NONE"`, so no authorizer context exists and only a key
verified here in process gets through.

A run's registry credential is the other key accepted: one the runs domain mints
per bundle with `runner:registry` under the run token tenant, for the runner to set
as `TF_TOKEN_<host>` during `terraform init`. It carries no user, so it is checked
here by scope and tenant rather than through the owner's live scopes, and it
reaches no other route.

Left out of the OpenAPI document, since the protocol is Terraform's contract
rather than this API's.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.responses import JSONResponse
from webbpulse.identity.api_keys import is_api_key, verify
from webbpulse.identity.scopes import FORBIDDEN_ERROR_CODE, bearer_credential, missing_scopes
from webbpulse.messages import forbidden

from ...common.core.auth import REGISTRY_READ, RUN_TOKEN_TENANT, RUNNER_REGISTRY_SCOPE, api_key_store, claims
from . import providers, service


def is_run_registry_credential(request: Request) -> bool:
    """Whether the bearer is a live registry credential a run was given."""
    presented = bearer_credential(request)
    if not presented or not is_api_key(presented):
        return False
    record = verify(presented, api_key_store())
    if record is None:
        return False
    return RUNNER_REGISTRY_SCOPE in record.scopes and record.tenant_id == RUN_TOKEN_TENANT


async def registry_reader(request: Request) -> None:
    """Admit a run's registry credential, or claims holding `registry:read`.

    Anything else is the 401 or 403 every other scoped route answers.
    """
    if is_run_registry_credential(request):
        return
    current = await claims(request)
    if missing_scopes(current, (REGISTRY_READ,)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": forbidden(), "error_code": FORBIDDEN_ERROR_CODE},
        )


router = APIRouter(
    prefix="/v1/modules",
    include_in_schema=False,
    dependencies=[Depends(registry_reader)],
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


providers_router = APIRouter(
    prefix="/v1/providers",
    include_in_schema=False,
    dependencies=[Depends(registry_reader)],
)

Platform = Annotated[str, Path(min_length=1, max_length=32, pattern=r"^[0-9a-z]+$")]


@providers_router.get("/{namespace}/{type}/versions")
def list_provider_versions(namespace: Segment, type: Segment) -> dict[str, Any]:  # noqa: A002
    """The provider's published versions with their protocols and platforms."""
    try:
        versions = providers.published_versions(namespace, type)
    except providers.ProviderNotFound as error:
        raise _not_found(error) from error
    return {"versions": versions}


@providers_router.get("/{namespace}/{type}/{version}/download/{os}/{arch}")
def download_provider(
    namespace: Segment,
    type: Segment,  # noqa: A002
    version: Version,
    os: Platform,
    arch: Platform,
) -> Response:
    """The zip, checksum list, signature and signing key Terraform verifies one platform's build against."""
    try:
        body = providers.download(namespace, type, version, os, arch)
    except providers.ProviderNotFound as error:
        raise _not_found(error) from error
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


__all__ = ["providers_router", "router"]
