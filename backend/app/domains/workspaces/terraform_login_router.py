"""The two halves of `terraform login`: the SPA's approval and the CLI's token exchange.

`POST /api/v1/oauth/authorizations` is called by the SPA's approve page for a
signed-in person, behind the step-up gate like minting a key by hand, and answers
the loopback redirect carrying a code. `POST /v1/oauth/token` is where Terraform
CLI exchanges that code; it carries no authorizer, since the code and its PKCE
verifier are the credential, and it speaks OAuth's form encoded request and JSON
error shape rather than this API's envelope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl

from anyio import to_thread
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from webbpulse.audit import AuditActor, AuditTarget
from webbpulse.http import client_ip
from webbpulse.identity.scopes import is_api_key_actor

from ...common import audit
from ...common.core.auth import recent_auth
from . import api_keys_service
from . import terraform_login as service
from .api_keys_router import KEY_ACTOR_CODE
from .schemas.terraform_login import LoginAuthorization, LoginAuthorizationCreate

if TYPE_CHECKING:  # pragma: no cover
    from webbpulse.identity.claims import AuthorizerClaims

LOGIN_REFUSED_CODE = "TERRAFORM_LOGIN_REFUSED"
"""The code the approve page matches on when the request is not one it can approve."""

MAX_TOKEN_BODY_BYTES = 8192
"""A token request is a handful of short fields; anything larger is refused unread."""

router = APIRouter()

token_router = APIRouter(include_in_schema=False)


@router.post(
    "/oauth/authorizations",
    response_model=LoginAuthorization,
    status_code=status.HTTP_201_CREATED,
)
def create_authorization(
    payload: LoginAuthorizationCreate,
    current: "AuthorizerClaims" = Depends(recent_auth),
) -> dict[str, Any]:
    """Approve a `terraform login` request and return where to send the browser.

    A signed-in person only: a key approving a login would mint a key, which is the
    same refusal the API keys route makes.
    """
    if is_api_key_actor(current):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "An API key may not approve a Terraform login.", "error_code": KEY_ACTOR_CODE},
        )
    user_id = api_keys_service.caller_user_id(current)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Authentication is required.", "error_code": "UNAUTHORIZED"},
        )
    try:
        redirect_url = service.authorize(
            user_id=user_id,
            held_scopes=api_keys_service.caller_scopes(current),
            client_id=payload.client_id,
            response_type=payload.response_type,
            redirect_uri=payload.redirect_uri,
            code_challenge=payload.code_challenge,
            code_challenge_method=payload.code_challenge_method,
            state=payload.state,
        )
    except service.LoginRefused as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": error.description, "error_code": LOGIN_REFUSED_CODE, "oauth_error": error.error},
        ) from error
    return {"redirect_url": redirect_url, "scopes": list(service.login_scopes(api_keys_service.caller_scopes(current)))}


def _oauth_error(error: service.LoginRefused) -> JSONResponse:
    """An RFC 6749 error body, 401 for a client mismatch and 400 otherwise."""
    code = status.HTTP_401_UNAUTHORIZED if error.error == "invalid_client" else status.HTTP_400_BAD_REQUEST
    return JSONResponse(
        {"error": error.error, "error_description": error.description},
        status_code=code,
        headers={"Cache-Control": "no-store"},
    )


@token_router.post("/v1/oauth/token")
async def token(request: Request) -> JSONResponse:
    """Exchange a code for the login key, as Terraform CLI's oauth2 client sends it.

    Go's oauth2 first sends the client id as HTTP Basic and retries in the body, so
    both are read. The key is minted in a worker thread since the store is blocking.
    """
    body = await request.body()
    if len(body) > MAX_TOKEN_BODY_BYTES:
        return _oauth_error(service.LoginRefused("invalid_request", "The request body is too large."))
    try:
        form = dict(parse_qsl(body.decode("utf-8"), max_num_fields=16))
    except (UnicodeDecodeError, ValueError):
        return _oauth_error(service.LoginRefused("invalid_request", "The body must be form encoded."))
    client_id = form.get("client_id") or service.basic_client_id(request.headers.get("authorization", ""))
    try:
        issued = await to_thread.run_sync(
            lambda: service.exchange(
                grant_type=form.get("grant_type", ""),
                code=form.get("code", ""),
                client_id=client_id,
                redirect_uri=form.get("redirect_uri", ""),
                code_verifier=form.get("code_verifier", ""),
            )
        )
    except service.LoginRefused as error:
        return _oauth_error(error)
    if issued.record is not None:
        audit.record(
            audit.API_KEY_CREATED,
            request=request,
            claims=None,
            actor=AuditActor(id=issued.record.user_id, kind="user", source="cli", ip=client_ip(request)),
            target=AuditTarget(type=audit.API_KEY, id=issued.record.key_hash, label=issued.record.name),
            payload={
                "scopes": list(issued.record.scopes),
                "expires_at": issued.record.expires_at or None,
                "no_expiry": False,
            },
        )
    return JSONResponse(
        {"access_token": issued.access_token, "token_type": "Bearer"},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


__all__ = ["LOGIN_REFUSED_CODE", "router", "token_router"]
