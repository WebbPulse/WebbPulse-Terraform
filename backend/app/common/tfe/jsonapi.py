"""JSON:API documents, errors and pagination as go-tfe parses them.

go-tfe decodes every response with `hashicorp/jsonapi`, which refuses a resource
whose `type` differs from the struct it fills, reads relationship linkage only as
`{"type", "id"}`, and turns an error body into its message only when it is a
JSON:API `errors` array. Everything here exists to keep those three exact.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any, Final

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.responses import Response

CONTENT_TYPE: Final = "application/vnd.api+json"
"""The media type go-tfe sends and HCP answers with."""

DEFAULT_PAGE_SIZE: Final = 20
"""HCP's default `page[size]`."""

MAX_PAGE_SIZE: Final = 100
"""HCP's ceiling on `page[size]`."""


class JsonApiResponse(Response):
    """A response carrying a JSON:API document with HCP's media type."""

    media_type = CONTENT_TYPE

    def render(self, content: Any) -> bytes:
        """Serialize compactly, as the API does everywhere else."""
        return json.dumps(content, separators=(",", ":"), default=str).encode()


def timestamp(value: Any) -> str | None:
    """A stored ISO time in the RFC 3339 form go-tfe's `iso8601` fields parse, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def linkage(resource_type: str, resource_id: str | None) -> dict[str, Any]:
    """A to-one relationship, with `data` null when nothing is related."""
    return {"data": {"type": resource_type, "id": resource_id} if resource_id else None}


def resource(
    resource_type: str,
    resource_id: str,
    attributes: Mapping[str, Any],
    *,
    relationships: Mapping[str, Any] | None = None,
    links: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One JSON:API resource object."""
    body: dict[str, Any] = {"id": resource_id, "type": resource_type, "attributes": dict(attributes)}
    if relationships:
        body["relationships"] = dict(relationships)
    if links:
        body["links"] = dict(links)
    return body


def document(
    data: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    included: Sequence[Mapping[str, Any]] | None = None,
    meta: Mapping[str, Any] | None = None,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> JsonApiResponse:
    """A top level document around one resource or a list of them."""
    body: dict[str, Any] = {"data": data if isinstance(data, Mapping) or data is None else list(data)}
    if included:
        body["included"] = list(included)
    if meta:
        body["meta"] = dict(meta)
    return JsonApiResponse(body, status_code=status_code, headers=dict(headers or {}))


def page_params(request: Request) -> tuple[int, int]:
    """The requested `page[number]` and `page[size]`, clamped to HCP's bounds."""

    def read(name: str, default: int) -> int:
        """One integer query parameter, or the default when absent or malformed."""
        try:
            return int(request.query_params.get(name, default))
        except ValueError:
            return default

    number = max(1, read("page[number]", 1))
    size = min(MAX_PAGE_SIZE, max(1, read("page[size]", DEFAULT_PAGE_SIZE)))
    return number, size


def paginate(items: Sequence[Any], request: Request) -> tuple[list[Any], dict[str, Any]]:
    """One page of `items` and the `meta.pagination` block go-tfe walks pages with."""
    number, size = page_params(request)
    total = len(items)
    pages = max(1, -(-total // size))
    start = (number - 1) * size
    pagination = {
        "current-page": number,
        "page-size": size,
        "prev-page": number - 1 if number > 1 else None,
        "next-page": number + 1 if number < pages else None,
        "total-pages": pages,
        "total-count": total,
    }
    return list(items[start : start + size]), {"pagination": pagination}


def error_document(status_code: int, detail: str, *, title: str | None = None) -> JsonApiResponse:
    """The JSON:API `errors` body go-tfe renders as `title` then `detail`."""
    phrase = title or HTTPStatus(status_code).phrase.lower()
    return JsonApiResponse(
        {"errors": [{"status": str(status_code), "title": phrase, "detail": detail}]},
        status_code=status_code,
    )


def _detail_text(detail: Any) -> str:
    """The human message out of an `HTTPException` detail in this API's envelope."""
    if isinstance(detail, Mapping):
        return str(detail.get("message") or detail.get("detail") or "")
    return str(detail or "")


class JsonApiRoute(APIRoute):
    """A route class answering every failure as a JSON:API error document.

    The rest of the API answers with the detailed envelope, which go-tfe cannot read,
    so a refusal from a scope check, a missing resource or a malformed body would reach
    the CLI as a bare status line. The route class wraps the handler, so failures in
    dependencies are caught too.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """Wrap the stock handler so its exceptions render as JSON:API errors."""
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            """Run the stock handler, rendering a refusal for go-tfe."""
            try:
                return await handler(request)
            except HTTPException as error:
                response = error_document(error.status_code, _detail_text(error.detail))
                for name, value in (error.headers or {}).items():
                    response.headers[name] = value
                return response
            except RequestValidationError as error:
                fields = ", ".join(".".join(str(part) for part in entry.get("loc", ())) for entry in error.errors())
                return error_document(422, f"Invalid request: {fields}.", title="invalid attribute")

        return guarded


def not_found(what: str = "resource") -> HTTPException:
    """The 404 go-tfe maps to `ErrResourceNotFound`."""
    return HTTPException(status_code=404, detail={"message": f"The {what} was not found.", "error_code": "NOT_FOUND"})


def unprocessable(message: str) -> HTTPException:
    """A 422 whose message go-tfe shows the person verbatim."""
    return HTTPException(status_code=422, detail={"message": message, "error_code": "INVALID"})


def conflict(message: str) -> HTTPException:
    """A 409, which go-tfe maps by path for the lock actions and shows otherwise."""
    return HTTPException(status_code=409, detail={"message": message, "error_code": "CONFLICT"})


async def request_attributes(request: Request) -> tuple[dict[str, Any], dict[str, Any]]:
    """The `data.attributes` and `data.relationships` of a JSON:API request body.

    Read by hand because go-tfe sends `application/vnd.api+json`, and because a body
    that is not a document is a 422 the CLI can show rather than a framework error.
    """
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError as error:
        raise unprocessable("The request body is not valid JSON.") from error
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, Mapping):
        raise unprocessable("The request body has no `data` object.")
    attributes = data.get("attributes") or {}
    relationships = data.get("relationships") or {}
    if not isinstance(attributes, Mapping) or not isinstance(relationships, Mapping):
        raise unprocessable("The request body's `attributes` and `relationships` must be objects.")
    return dict(attributes), dict(relationships)


def related_id(relationships: Mapping[str, Any], name: str) -> str | None:
    """The id a request's to-one relationship names, or None."""
    entry = relationships.get(name)
    data = entry.get("data") if isinstance(entry, Mapping) else None
    if isinstance(data, Mapping) and data.get("id"):
        return str(data["id"])
    return None


def now() -> str:
    """The current time in go-tfe's `iso8601` form."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
