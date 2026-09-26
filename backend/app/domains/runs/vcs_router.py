"""The GitHub Actions upload route, authenticated by the job's own OIDC token.

It carries no identity JWT and no `wpk_` key, and is exposed past the staging
gate with `authorization_type = "NONE"` like the runner routes, because a
workflow has neither. The GitHub token is verified here, in process, against
GitHub's published keys.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from webbpulse.identity.scopes import bearer_credential

from . import vcs
from .schemas.vcs import VcsUpload, VcsUploadCreate

router = APIRouter()

VCS_REPO_NOT_BOUND_CODE = "VCS_REPO_NOT_BOUND"
"""The code the workflow treats as a notice rather than a failure."""

VCS_EVENT_UNSUPPORTED_CODE = "VCS_EVENT_UNSUPPORTED"


def _error(status_code: int, message: str, error_code: str, **headers: str) -> HTTPException:
    """An error whose body carries a top-level `error_code` the workflow matches on."""
    return HTTPException(
        status_code=status_code,
        detail={"message": message, "error_code": error_code},
        headers=headers or None,
    )


@router.post(
    "/vcs/uploads",
    response_model=VcsUpload,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "The GitHub Actions OIDC token is missing or did not verify."},
        404: {"description": "No workspace is bound to the repository. `error_code` is `VCS_REPO_NOT_BOUND`."},
        422: {"description": "The token is for neither a branch push nor a pull request."},
    },
)
def create_vcs_upload(payload: VcsUploadCreate, request: Request) -> dict[str, Any]:
    """Issue a presigned PUT for a workflow's configuration tarball.

    The caller sends `Authorization: Bearer <GitHub Actions OIDC token>`. The
    repository, event, branch and pull request number come from its verified
    claims. A retry of the same workflow run attempt returns the same `upload_id`
    with a new URL for the same key.
    """
    try:
        return vcs.issue_upload(bearer_credential(request), payload.model_dump())
    except vcs.InvalidUploadToken as error:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication is required.",
            "UNAUTHORIZED",
            **{"WWW-Authenticate": "Bearer"},
        ) from error
    except vcs.UnsupportedEvent as error:
        raise _error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(error), VCS_EVENT_UNSUPPORTED_CODE) from error
    except vcs.RepositoryNotBound as error:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            f"No workspace is connected to {error}.",
            VCS_REPO_NOT_BOUND_CODE,
        ) from error


__all__ = ["VCS_EVENT_UNSUPPORTED_CODE", "VCS_REPO_NOT_BOUND_CODE", "router"]
