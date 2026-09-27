"""The registry's routes under `/api/v1`: the GitHub Actions upload and the listing.

The upload carries no identity JWT and no `wpk_` key, and is exposed past the
staging gate with `authorization_type = "NONE"`, because a workflow has neither.
The GitHub token is verified here, in process, against GitHub's published keys.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from webbpulse.identity.scopes import bearer_credential

from ...common.core.auth import REGISTRY_READ, scopes
from . import service
from .schemas.registry import ModuleList, ModuleUpload, ModuleUploadCreate

router = APIRouter(prefix="/registry")

REPO_NOT_ALLOWED_CODE = "REGISTRY_REPO_NOT_ALLOWED"
REF_UNSUPPORTED_CODE = "REGISTRY_REF_UNSUPPORTED"
INVALID_MODULE_NAME_CODE = "REGISTRY_INVALID_MODULE_NAME"
VERSION_EXISTS_CODE = "REGISTRY_VERSION_EXISTS"


def _error(status_code: int, message: str, error_code: str, **headers: str) -> HTTPException:
    """An error whose body carries a top-level `error_code` the workflow matches on."""
    return HTTPException(
        status_code=status_code,
        detail={"message": message, "error_code": error_code},
        headers=headers or None,
    )


@router.post(
    "/uploads",
    response_model=ModuleUpload,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "The GitHub Actions OIDC token is missing or did not verify."},
        403: {"description": "The repository may not publish modules. `error_code` is `REGISTRY_REPO_NOT_ALLOWED`."},
        409: {"description": "The version is already published. `error_code` is `REGISTRY_VERSION_EXISTS`."},
        422: {"description": "The token is not for a version tag push, or the repository names no valid module."},
    },
)
def create_module_upload(payload: ModuleUploadCreate, request: Request) -> dict[str, Any]:
    """Issue a presigned PUT for a module version's tarball.

    The caller sends `Authorization: Bearer <GitHub Actions OIDC token>` from a
    workflow running on a `v<semver>` tag push. The module and version come from
    its verified claims. A retry of the same workflow run attempt returns the same
    `upload_id` with a new URL for the same key.
    """
    try:
        return service.issue_upload(bearer_credential(request), payload.size_bytes)
    except service.InvalidUploadToken as error:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication is required.",
            "UNAUTHORIZED",
            **{"WWW-Authenticate": "Bearer"},
        ) from error
    except service.RepositoryNotAllowed as error:
        raise _error(status.HTTP_403_FORBIDDEN, f"{error} may not publish modules.", REPO_NOT_ALLOWED_CODE) from error
    except service.UnsupportedRef as error:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error), REF_UNSUPPORTED_CODE) from error
    except service.InvalidModuleName as error:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error), INVALID_MODULE_NAME_CODE) from error
    except service.VersionAlreadyPublished as error:
        raise _error(status.HTTP_409_CONFLICT, f"{error} is already published.", VERSION_EXISTS_CODE) from error


@router.get("/modules", response_model=ModuleList, dependencies=[Depends(scopes(REGISTRY_READ))])
def list_modules() -> dict[str, Any]:
    """Every module and every version, with pending and failed ingests shown."""
    return {"modules": service.list_modules()}


__all__ = [
    "INVALID_MODULE_NAME_CODE",
    "REF_UNSUPPORTED_CODE",
    "REPO_NOT_ALLOWED_CODE",
    "VERSION_EXISTS_CODE",
    "router",
]
