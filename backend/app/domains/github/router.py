"""The GitHub domain's routes, every one of them admin only.

The two callbacks GitHub redirects to land on the SPA, which forwards their query here.
That keeps every route behind the authorizer: the state in the query is the CSRF check,
and the admin session is what ties the callback to the person who started the flow.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Response, status
from webbpulse.identity.claims import AuthorizerClaims
from webbpulse.integrations.github import (
    GitHubError,
    GitHubNotConfigured,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubUnprocessable,
)
from webbpulse.ops.config import ConfigToolError

from ...common.core.auth import ADMIN, claims, scopes
from . import service
from .schemas.github import (
    GitHubAppStatus,
    Installation,
    InstallationCallback,
    InstallationList,
    InstallStart,
    ManifestConversionRequest,
    ManifestStart,
    ManifestStartRequest,
    RepositoryList,
)

router = APIRouter(prefix="/github", dependencies=[Depends(scopes(ADMIN))])

InstallationId = Path(gt=0, le=2**53)

NOT_CONFIGURED = "GITHUB_APP_NOT_CONFIGURED"
ALREADY_CONFIGURED = "GITHUB_APP_ALREADY_CONFIGURED"
INVALID_STATE = "GITHUB_INVALID_STATE"
NOT_THIS_APP = "GITHUB_INSTALLATION_NOT_THIS_APP"
FRONTEND_URL_MISSING = "GITHUB_FRONTEND_URL_MISSING"
SLUG_MISSING = "GITHUB_APP_SLUG_MISSING"
GITHUB_UNAVAILABLE = "GITHUB_UNAVAILABLE"
CODE_REJECTED = "GITHUB_MANIFEST_CODE_REJECTED"
SECRET_WRITE_FAILED = "GITHUB_SECRET_WRITE_FAILED"


def _error(status_code: int, message: str, error_code: str) -> HTTPException:
    """An error carrying a stable code the SPA matches on."""
    return HTTPException(status_code=status_code, detail={"message": message, "error_code": error_code})


def _actor(current: Optional[AuthorizerClaims]) -> Optional[str]:
    """The caller's subject, recorded on what they create."""
    subject = str((current or {}).get("sub", "") or "").strip()
    return subject or None


@contextmanager
def _github_errors() -> Iterator[None]:
    """Translate the client's and the service's failures into this API's errors.

    GitHub's own message is never forwarded, since a credential exchange is among the
    calls that can fail.
    """
    try:
        yield
    except service.InvalidState as error:
        raise _error(400, "That link has expired or was already used. Start again.", INVALID_STATE) from error
    except service.AppAlreadyConfigured as error:
        raise _error(409, "This environment already has its GitHub App.", ALREADY_CONFIGURED) from error
    except service.FrontendUrlMissing as error:
        raise _error(409, "No frontend URL is configured for the callbacks.", FRONTEND_URL_MISSING) from error
    except service.SlugMissing as error:
        raise _error(409, "The GitHub App's slug is unknown, so it cannot be installed.", SLUG_MISSING) from error
    except service.InstallationNotFound as error:
        raise _error(404, "No such installation.", "NOT_FOUND") from error
    except GitHubNotConfigured as error:
        raise _error(409, "No GitHub App is configured for this environment.", NOT_CONFIGURED) from error
    except GitHubRateLimited as error:
        raise _error(503, "GitHub is rate limiting this App. Try again shortly.", GITHUB_UNAVAILABLE) from error
    except GitHubError as error:
        raise _error(502, "GitHub refused or failed the request.", GITHUB_UNAVAILABLE) from error
    except ConfigToolError as error:
        raise _error(500, "The App credentials could not be stored.", SECRET_WRITE_FAILED) from error


@router.get("/app", response_model=GitHubAppStatus)
def get_app() -> dict[str, Any]:
    """This environment's App, or the fact that there is none yet."""
    return service.app_status()


@router.post("/app/manifest", response_model=ManifestStart)
def start_manifest(
    payload: ManifestStartRequest,
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Issue a manifest state; the SPA then posts the manifest to `action_url`."""
    with _github_errors():
        return service.start_manifest(
            organization=payload.organization,
            name=payload.name,
            actor=_actor(current),
        )


@router.post("/app/conversions", response_model=GitHubAppStatus)
def convert_manifest(
    payload: ManifestConversionRequest,
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Finish creating the App from the code GitHub redirected back with."""
    with _github_errors():
        try:
            return service.complete_manifest(code=payload.code, state=payload.state, actor=_actor(current))
        except (GitHubNotFound, GitHubUnprocessable) as error:
            raise _error(400, "GitHub did not accept that code. Create the App again.", CODE_REJECTED) from error


@router.post("/install-state", response_model=InstallStart)
def start_install(current: AuthorizerClaims = Depends(claims)) -> dict[str, Any]:
    """Issue an install state and the GitHub URL that carries it."""
    with _github_errors():
        return service.start_install(actor=_actor(current))


@router.post("/installations", response_model=Installation, status_code=status.HTTP_201_CREATED)
def record_installation(payload: InstallationCallback) -> dict[str, Any]:
    """Store the installation the setup callback named, once GitHub confirms it."""
    with _github_errors():
        try:
            return service.record_installation(
                installation_id=payload.installation_id,
                setup_action=payload.setup_action,
                state=payload.state,
            )
        except GitHubNotFound as error:
            raise _error(400, "That installation does not belong to this GitHub App.", NOT_THIS_APP) from error


@router.get("/installations", response_model=InstallationList)
def list_installations() -> dict[str, Any]:
    """Every installation stored here."""
    return {"items": service.list_installations()}


@router.post("/installations/{installation_id}/refresh", response_model=Installation)
def refresh_installation(installation_id: int = InstallationId) -> dict[str, Any]:
    """Re-read one installation from GitHub; one GitHub no longer has is dropped."""
    with _github_errors():
        return service.refresh_installation(installation_id)


@router.delete("/installations/{installation_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_installation(installation_id: int = InstallationId) -> Response:
    """Forget one installation here. It stays installed on GitHub until removed there."""
    with _github_errors():
        service.remove_installation(installation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/installations/{installation_id}/repositories", response_model=RepositoryList)
def list_repositories(installation_id: int = InstallationId) -> dict[str, Any]:
    """Every repository one installation covers, as GitHub lists them now."""
    with _github_errors():
        return {"items": service.list_repositories(installation_id)}
