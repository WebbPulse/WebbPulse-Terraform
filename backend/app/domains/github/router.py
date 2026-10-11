"""The GitHub domain's routes, every one of them admin only.

The two callbacks GitHub redirects to land on the SPA, which forwards their query here.
That keeps every route behind the authorizer: the state in the query is the CSRF check,
and the admin session is what ties the callback to the person who started the flow.

Every change to the App's settings needs a login within the step-up window: starting
the manifest or an install, syncing the webhook and forgetting an installation. The
callbacks that finish a flow are bound to a state only such a start issued.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from webbpulse.audit import AuditTarget
from webbpulse.identity.claims import AuthorizerClaims
from webbpulse.integrations.github import (
    GitHubError,
    GitHubNotConfigured,
    GitHubNotFound,
    GitHubRateLimited,
    GitHubUnprocessable,
)
from webbpulse.ops.config import ConfigToolError

from ...common import audit
from ...common.core.auth import ADMIN, claims, recent_auth, scopes
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
    WebhookConfig,
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
WEBHOOK_URL_MISSING = "GITHUB_WEBHOOK_URL_MISSING"
WEBHOOK_SECRET_MISSING = "GITHUB_WEBHOOK_SECRET_MISSING"


def _error(status_code: int, message: str, error_code: str) -> HTTPException:
    """An error carrying a stable code the SPA matches on."""
    return HTTPException(status_code=status_code, detail={"message": message, "error_code": error_code})


def _actor(current: Optional[AuthorizerClaims]) -> Optional[str]:
    """The caller's subject, recorded on what they create."""
    subject = str((current or {}).get("sub", "") or "").strip()
    return subject or None


def _app_target(app: Optional[dict[str, Any]] = None) -> AuditTarget:
    """This environment's App as an audit target, labelled by its slug where known."""
    current = app or {}
    return AuditTarget(
        type=audit.GITHUB_APP,
        id=str(current.get("app_id") or "github-app"),
        label=str(current.get("slug") or ""),
    )


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
    except service.WebhookUrlMissing as error:
        raise _error(409, "No API URL is configured for the webhook.", WEBHOOK_URL_MISSING) from error
    except service.WebhookSecretMissing as error:
        raise _error(409, "The app secret holds no webhook secret.", WEBHOOK_SECRET_MISSING) from error
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
    service.refresh_app()
    return service.app_status()


@router.post("/app/manifest", response_model=ManifestStart)
def start_manifest(
    payload: ManifestStartRequest,
    current: AuthorizerClaims = Depends(recent_auth),
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
    request: Request,
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Finish creating the App from the code GitHub redirected back with."""
    with _github_errors():
        try:
            app = service.complete_manifest(code=payload.code, state=payload.state, actor=_actor(current))
        except (GitHubNotFound, GitHubUnprocessable) as error:
            raise _error(400, "GitHub did not accept that code. Create the App again.", CODE_REJECTED) from error
    audit.record(
        audit.GITHUB_APP_CREATED,
        request=request,
        claims=current,
        target=_app_target(app),
        payload={"app_slug": app.get("slug")},
    )
    return app


@router.post("/app/webhook", response_model=WebhookConfig)
def configure_webhook(request: Request, current: AuthorizerClaims = Depends(recent_auth)) -> dict[str, Any]:
    """Point the App's webhook at this API and set its secret from the `app` secret."""
    with _github_errors():
        config = service.sync_webhook()
    audit.record(audit.GITHUB_WEBHOOK_SYNCED, request=request, claims=current, target=_app_target())
    return config


@router.post("/install-state", response_model=InstallStart)
def start_install(current: AuthorizerClaims = Depends(recent_auth)) -> dict[str, Any]:
    """Issue an install state and the GitHub URL that carries it."""
    with _github_errors():
        return service.start_install(actor=_actor(current))


@router.post("/installations", response_model=Installation, status_code=status.HTTP_201_CREATED)
def record_installation(
    payload: InstallationCallback,
    request: Request,
    current: AuthorizerClaims = Depends(claims),
) -> dict[str, Any]:
    """Store the installation the setup callback named, once GitHub confirms it."""
    with _github_errors():
        try:
            installation = service.record_installation(
                installation_id=payload.installation_id,
                setup_action=payload.setup_action,
                state=payload.state,
            )
        except GitHubNotFound as error:
            raise _error(400, "That installation does not belong to this GitHub App.", NOT_THIS_APP) from error
    audit.record(
        audit.GITHUB_INSTALLATION_RECORDED,
        request=request,
        claims=current,
        target=_app_target(),
        payload={"installation_id": payload.installation_id},
    )
    return installation


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
def remove_installation(
    request: Request,
    installation_id: int = InstallationId,
    current: AuthorizerClaims = Depends(recent_auth),
) -> Response:
    """Forget one installation here. It stays installed on GitHub until removed there."""
    with _github_errors():
        service.remove_installation(installation_id)
    audit.record(
        audit.GITHUB_INSTALLATION_REMOVED,
        request=request,
        claims=current,
        target=_app_target(),
        payload={"installation_id": installation_id},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/installations/{installation_id}/repositories", response_model=RepositoryList)
def list_repositories(installation_id: int = InstallationId) -> dict[str, Any]:
    """Every repository one installation covers, as GitHub lists them now."""
    with _github_errors():
        return {"items": service.list_repositories(installation_id)}
