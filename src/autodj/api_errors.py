"""Render public domain and request-validation errors at the HTTP boundary."""

from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from autodj.errors import FieldError, first_issue


def error_headers[T: Callable[..., Any]](
    headers: Mapping[str, str], *, error_type: type[FieldError] = FieldError
) -> Callable[[T], T]:
    """Keep a route's error cache policy with its endpoint, including station aliases."""

    def decorate(endpoint: T) -> T:
        setattr(endpoint, "__field_error_headers__", (error_type, dict(headers)))  # noqa: B010
        return endpoint

    return decorate


async def field_error_response(request: Request, exc: Exception) -> JSONResponse:
    """Return public words without echoing rejected input, including NaN or secrets."""
    headers = None
    if isinstance(exc, RequestValidationError):
        location = exc.errors()[0].get("loc", ())
        has_source = bool(location) and location[0] in {"body", "query", "path", "header", "cookie"}
        position = 1 if has_source else 0
        has_field = len(location) > position and isinstance(location[position], str)
        field, message = first_issue(
            exc, {} if has_field else None, location_index=position if has_field else 0
        )
        response_field = field if has_field or field not in location else None
        exc = FieldError(message, response_field, status=422, include_field=bool(response_field))
    else:
        policy = getattr(request.scope.get("endpoint"), "__field_error_headers__", None)
        if policy is not None and isinstance(exc, policy[0]):
            headers = policy[1]
    assert isinstance(exc, FieldError)
    content: dict[str, str | None] = {"detail": exc.message}
    if exc.include_field:
        content["field"] = (exc.field or None) if exc.null_empty_field else exc.field
    return JSONResponse(content, status_code=exc.status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """Install the same renderer for domain refusals and FastAPI validation."""
    app.add_exception_handler(FieldError, field_error_response)
    app.add_exception_handler(RequestValidationError, field_error_response)
