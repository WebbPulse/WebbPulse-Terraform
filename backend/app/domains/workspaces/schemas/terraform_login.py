"""Request and response models for approving a `terraform login`."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LoginAuthorizationCreate(BaseModel):
    """The query Terraform CLI opened the approve page with, forwarded as is."""

    client_id: str = Field(max_length=64)
    response_type: str = Field(max_length=16)
    redirect_uri: str = Field(max_length=256)
    code_challenge: str = Field(max_length=128)
    code_challenge_method: str = Field(max_length=16)
    state: str = Field(default="", max_length=512)


class LoginAuthorization(BaseModel):
    """Where to send the browser, and the scopes the new key will carry."""

    redirect_url: str
    """Terraform's loopback listener with the code and state appended."""
    scopes: list[str]


__all__ = ["LoginAuthorization", "LoginAuthorizationCreate"]
