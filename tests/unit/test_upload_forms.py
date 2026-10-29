"""Bounds and cleanup for shared multipart audio-upload extraction."""

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData, UploadFile

from autodj.upload_forms import read_single_file_form


@pytest.mark.parametrize("asset", ["spot", "intro"])
async def test_reports_asset_specific_malformed_multipart(asset: str) -> None:
    request = SimpleNamespace(form=AsyncMock(side_effect=ValueError("bad multipart")))

    with pytest.raises(HTTPException, match=f"Send the {asset} as multipart/form-data"):
        await read_single_file_form(request, asset)

    request.form.assert_awaited_once_with(max_files=1, max_fields=16, max_part_size=4096)


async def test_closes_upload_when_its_field_is_not_file() -> None:
    upload = UploadFile(io.BytesIO(b"audio"), filename="clip.wav")
    form = FormData([("title", upload)])
    request = SimpleNamespace(form=AsyncMock(return_value=form))

    with pytest.raises(HTTPException, match="one audio file"):
        await read_single_file_form(request, "spot")

    assert upload.file.closed


async def test_returns_file_and_text_fields() -> None:
    upload = UploadFile(io.BytesIO(b"audio"), filename="clip.wav")
    form = SimpleNamespace(
        get=lambda _key: upload,
        multi_items=lambda: [("title", "Test"), ("file", upload)],
        close=AsyncMock(),
    )
    request = SimpleNamespace(form=AsyncMock(return_value=form))

    values, actual_upload = await read_single_file_form(request, "intro")

    assert values == {"title": "Test"}
    assert actual_upload is upload
