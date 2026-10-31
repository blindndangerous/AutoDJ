"""Shared validation extraction preserves domain fields and public messages."""

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


def test_nested_field_error_keeps_its_field_instead_of_the_model_location():
    from autodj.errors import FieldError, first_issue

    class Checked(BaseModel):
        @model_validator(mode="after")
        def check(self):
            raise FieldError("Dates are reversed", "not_after", 409)

    class Form(BaseModel):
        dates: Checked

    with pytest.raises(ValidationError) as caught:
        Form(dates={})
    assert first_issue(caught.value, {"dates": "Dates"}) == ("not_after", "Dates are reversed")


def test_first_issue_handles_unknown_fields_and_request_locations():
    from autodj.errors import first_issue

    class Form(BaseModel):
        model_config = ConfigDict(extra="forbid")

    with pytest.raises(ValidationError) as caught:
        Form(unexpected=True)
    assert first_issue(caught.value) == ("unexpected", "Unknown field unexpected")
    request = RequestValidationError(
        [{"type": "missing", "loc": ("body", "name"), "msg": "Field required"}]
    )
    assert first_issue(request, {"name": "Name"}, location_index=-1) == (
        "name",
        "Name: Field required",
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, ("", "Value error, invalid")),
        ({"fallback_field": "changes", "strip_value_error": True}, ("changes", "invalid")),
        ({"fallback_label": "Dates"}, ("", "Dates: Value error, invalid")),
    ],
)
def test_first_issue_preserves_each_form_root_error_policy(options, expected):
    from autodj.errors import first_issue

    error = RequestValidationError([{"loc": (), "msg": "Value error, invalid"}])
    assert first_issue(error, **options) == expected


def test_first_issue_can_keep_raw_unknown_field_messages_and_optional_labels():
    from autodj.errors import first_issue

    error = RequestValidationError(
        [{"loc": ("clips",), "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}]
    )
    assert first_issue(error, {"name": "Name"}, prefix_unlabelled=False, unknown_field=False) == (
        "clips",
        "Extra inputs are not permitted",
    )


@pytest.mark.parametrize("location", [("name", "unexpected"), ("params", "unexpected")])
def test_schedule_unknown_field_takes_priority_over_field_specific_wording(location):
    from autodj.scheduler import validation_issue

    error = ValidationError.from_exception_data(
        "Schedule", [{"loc": location, "type": "extra_forbidden", "input": True}]
    )
    assert validation_issue(error) == (location[0], f"Unknown field {location[0]}")


@pytest.mark.parametrize("name", ["name", "target_minutes", "songs"])
def test_show_unknown_field_takes_priority_over_field_specific_wording(name):
    from autodj.shows import validation_error

    error = ValidationError.from_exception_data(
        "Show", [{"loc": (name, "unexpected"), "type": "extra_forbidden", "input": True}]
    )
    result = validation_error(error)
    assert (result.field, result.message) == (name, f"Unknown field {name}")


@pytest.mark.parametrize(
    ("module", "name", "args", "status", "field"),
    [
        ("ads", "AdsError", ("name", "Invalid"), 422, "name"),
        ("categories", "CategoryError", ("name", "Invalid"), 400, "name"),
        ("clip_dates", "ClipDatesError", ("not_after", "Invalid"), 400, "not_after"),
        ("matched_intros", "IntroError", ("", "Invalid"), 422, ""),
        ("scheduler", "EventError", ("params.name", "Invalid"), 400, "params.name"),
        ("shows", "ShowError", ("name", "Invalid"), 400, "name"),
        ("shows", "ShowBusy", ("", "Invalid"), 409, ""),
        ("review_jobs", "ReviewError", ("Invalid",), 400, None),
        ("tag_edit", "TagError", ("Invalid",), 400, None),
        ("tag_edit", "TagWriteError", ("Invalid",), 409, None),
        ("tag_edit", "TagFileBusyError", ("Invalid",), 409, None),
    ],
)
def test_domain_error_is_handled_by_shared_base_without_losing_its_contract(
    module, name, args, status, field
):
    import importlib

    from autodj.errors import FieldError

    error = getattr(importlib.import_module(f"autodj.{module}"), name)(*args)
    assert isinstance(error, FieldError)
    assert (str(error), error.message, error.field, error.status) == (
        "Invalid",
        "Invalid",
        field,
        status,
    )
    assert error.include_field is (name not in {"ReviewError", "TagWriteError", "TagFileBusyError"})
