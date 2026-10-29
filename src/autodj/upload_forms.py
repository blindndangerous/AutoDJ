"""Shared extraction for station audio-upload forms."""

from __future__ import annotations

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile


async def read_single_file_form(request: Request, asset: str) -> tuple[dict[str, str], UploadFile]:
    """Read one upload and short text fields, closing malformed forms."""
    try:
        form = await request.form(max_files=1, max_fields=16, max_part_size=4096)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Send the {asset} as multipart/form-data"
        ) from exc
    upload = form.get("file")
    values = {key: value for key, value in form.multi_items() if key != "file"}
    if not isinstance(upload, UploadFile) or any(
        not isinstance(value, str) for value in values.values()
    ):
        await form.close()
        raise HTTPException(status_code=422, detail="Send one audio file as the field file")
    return {key: str(value) for key, value in values.items()}, upload
