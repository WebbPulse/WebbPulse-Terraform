"""The registry's routes under `/api/v1`: connecting modules to repositories, and the listing.

Creating and deleting a module needs `registry:write`, which only an admin holds;
reading needs `registry:read`. Publishing has no route: a semantic version tag
pushed to a connected repository arrives through the GitHub App's webhook, and
connecting or resyncing a module imports the tags the repository already holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from webbpulse.identity.claims import AuthorizerClaims
from webbpulse.integrations.github import GitHubError, GitHubRateLimited

from ...common.core.auth import REGISTRY_READ, REGISTRY_WRITE, claims, scopes
from ...common.github.repositories import RepositoryNotInstalled
from . import service
from .schemas.registry import Module, ModuleCreate, ModuleList, ModuleSync

router = APIRouter(prefix="/registry")

INVALID_MODULE_NAME_CODE = "REGISTRY_INVALID_MODULE_NAME"
MODULE_EXISTS_CODE = "REGISTRY_MODULE_EXISTS"
NOT_FOUND_CODE = "REGISTRY_NOT_FOUND"
VCS_REPO_NOT_INSTALLED_CODE = "VCS_REPO_NOT_INSTALLED"
GITHUB_UNAVAILABLE_CODE = "GITHUB_UNAVAILABLE"
GITHUB_NOT_CONFIGURED_CODE = "GITHUB_NOT_CONFIGURED"
SYNC_UNAVAILABLE_CODE = "REGISTRY_SYNC_UNAVAILABLE"

Segment = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z_-]+$")]


def _error(status_code: int, message: str, error_code: str) -> HTTPException:
    """An error whose body carries a stable `error_code`."""
    return HTTPException(status_code=status_code, detail={"message": message, "error_code": error_code})


def _actor(current: Optional[AuthorizerClaims]) -> Optional[str]:
    """The caller's subject, recorded on what they create."""
    subject = str((current or {}).get("sub", "") or "").strip()
    return subject or None


@contextmanager
def _connect_errors() -> Iterator[None]:
    """Translate a failed repository resolution into this API's errors.

    GitHub's own message is never forwarded, since the resolution runs on App credentials.
    """
    try:
        yield
    except service.RegistryUnavailable as error:
        raise _error(
            status.HTTP_409_CONFLICT, "Create the GitHub App before connecting a module.", GITHUB_NOT_CONFIGURED_CODE
        ) from error
    except RepositoryNotInstalled as error:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"The GitHub App is not installed on {error}. Install it on the repository first.",
            VCS_REPO_NOT_INSTALLED_CODE,
        ) from error
    except GitHubRateLimited as error:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "GitHub is rate limiting this App. Try again shortly.",
            GITHUB_UNAVAILABLE_CODE,
        ) from error
    except GitHubError as error:
        raise _error(
            status.HTTP_502_BAD_GATEWAY, "GitHub could not resolve the repository.", GITHUB_UNAVAILABLE_CODE
        ) from error


@router.post(
    "/modules",
    response_model=Module,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(scopes(REGISTRY_WRITE))],
    responses={
        409: {"description": "A module already sits at the address, or there is no GitHub App."},
        422: {"description": "The App is not installed on the repository, or the address is not valid."},
    },
)
def create_module(payload: ModuleCreate, current: AuthorizerClaims = Depends(claims)) -> dict[str, Any]:
    """Connect a module to a repository; each `vX.Y.Z` or `X.Y.Z` tag pushed there publishes it.

    The repository's existing semantic version tags are imported in the
    background unless `import_tags` is false. Other tags are ignored.
    """
    with _connect_errors():
        try:
            return service.create_module(
                payload.vcs_repo, payload.name, payload.provider, _actor(current), import_tags=payload.import_tags
            )
        except service.InvalidModuleName as error:
            raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error), INVALID_MODULE_NAME_CODE) from error
        except service.ModuleExists as error:
            raise _error(status.HTTP_409_CONFLICT, f"{error} already exists.", MODULE_EXISTS_CODE) from error


@router.get("/modules", response_model=ModuleList, dependencies=[Depends(scopes(REGISTRY_READ))])
def list_modules() -> dict[str, Any]:
    """Every module and every version, with pending and failed versions shown."""
    return {"modules": service.list_modules()}


@router.get(
    "/modules/{namespace}/{name}/{provider}",
    response_model=Module,
    dependencies=[Depends(scopes(REGISTRY_READ))],
    responses={404: {"description": "No module sits at the address."}},
)
def get_module(namespace: Segment, name: Segment, provider: Segment) -> dict[str, Any]:
    """One module and every version of it."""
    try:
        return service.get_module(namespace, name, provider)
    except service.ModuleNotFound as error:
        raise _error(status.HTTP_404_NOT_FOUND, f"{error} was not found.", NOT_FOUND_CODE) from error


@router.post(
    "/modules/{namespace}/{name}/{provider}/resync",
    response_model=ModuleSync,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(scopes(REGISTRY_WRITE))],
    responses={
        404: {"description": "No module connected to a repository sits at the address."},
        503: {"description": "The sync could not be queued."},
    },
)
def resync_module(
    namespace: Segment, name: Segment, provider: Segment, current: AuthorizerClaims = Depends(claims)
) -> dict[str, str]:
    """Queue an import of every semantic version tag in the module's repository.

    Picks up tags pushed before the module was connected, or pushed more than
    three at once, when GitHub sends no push event. Published versions are left alone.
    """
    try:
        return service.resync_module(namespace, name, provider, _actor(current))
    except service.ModuleNotFound as error:
        raise _error(status.HTTP_404_NOT_FOUND, f"{error} was not found.", NOT_FOUND_CODE) from error
    except service.SyncUnavailable as error:
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "The tag sync could not be queued.", SYNC_UNAVAILABLE_CODE
        ) from error


@router.delete(
    "/modules/{namespace}/{name}/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(scopes(REGISTRY_WRITE))],
    responses={404: {"description": "No module sits at the address."}},
)
def delete_module(namespace: Segment, name: Segment, provider: Segment) -> Response:
    """Remove a module with every version and stored tarball. Configurations pinned to it stop resolving."""
    try:
        service.delete_module(namespace, name, provider)
    except service.ModuleNotFound as error:
        raise _error(status.HTTP_404_NOT_FOUND, f"{error} was not found.", NOT_FOUND_CODE) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = [
    "GITHUB_NOT_CONFIGURED_CODE",
    "GITHUB_UNAVAILABLE_CODE",
    "INVALID_MODULE_NAME_CODE",
    "MODULE_EXISTS_CODE",
    "NOT_FOUND_CODE",
    "SYNC_UNAVAILABLE_CODE",
    "VCS_REPO_NOT_INSTALLED_CODE",
    "router",
]
