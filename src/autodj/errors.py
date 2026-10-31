"""Public domain errors and first-issue extraction, independent of the web layer."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol


class FieldError(ValueError):
    """A public refusal with an optional form field and response status."""

    def __init__(
        self,
        message: str,
        field: str | None = None,
        status: int = 400,
        *,
        include_field: bool = True,
        null_empty_field: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.status = status
        self.include_field = include_field
        self.null_empty_field = null_empty_field


class ValidationIssues(Protocol):
    """The error interface shared by Pydantic and request validation."""

    def errors(self) -> Sequence[Mapping[str, Any]]: ...


def first_issue(
    exc: ValidationIssues,
    labels: Mapping[str, str] | None = None,
    *,
    fallback_field: str = "",
    fallback_label: str | None = None,
    location_index: int = 0,
    prefix_unlabelled: bool = True,
    strip_value_error: bool = False,
    unknown_field: bool = True,
) -> tuple[str, str]:
    """Extract one problem, retaining the caller's field and wording conventions.

    Embedded domain errors already contain public wording. Otherwise, labels
    prefix messages only when supplied; callers may select the last location
    component for request errors and retain Pydantic's unknown-field wording.
    """
    error = exc.errors()[0]
    cause = (error.get("ctx") or {}).get("error")
    if isinstance(cause, FieldError):
        return cause.field or "", cause.message
    location = error.get("loc", ())
    name = str(location[location_index]) if location else fallback_field
    if unknown_field and error.get("type") == "extra_forbidden":
        return name, f"Unknown field {name}".rstrip()
    message = str(error.get("msg", "is not valid"))
    if strip_value_error:
        message = message.removeprefix("Value error, ")
    if labels is not None and (prefix_unlabelled or name in labels):
        label = labels.get(name, name or fallback_label or "")
        message = f"{label}: {message}"
    elif not location and fallback_label is not None:
        message = f"{fallback_label}: {message}"
    return name, message
