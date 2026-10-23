"""Names vulture reports as unused that are used, read by vulture via pyproject.toml.

Add a name here only with the reason it is a false positive.  Code that only
tests use is dead: delete it, and move what the tests need into tests/.
"""

# ruff: noqa: B018, F821

# sqlite3.Connection attribute.
_.row_factory
# shlex.shlex attributes.
_.whitespace_split
_.commenters
# numpy complex array component, assigned in place.
_.imag
# ctypes structure field passed to SetInformationJobObject.
_.LimitFlags
# urllib.request.HTTPRedirectHandler override.
_.redirect_request
# torch.nn.Module and transformers hooks.
_.forward
_._attn_implementation
# starlette Convertor and StaticFiles overrides.
_.regex
_.to_string
_.file_response
# Pydantic and dataclass fields: part of a request body, saved file or record.
_.reviewed_revision
_.last_scheduled
_.published_at
_.segue_s
_.incoming_s
_.pure_shuffle
_.anchor_to_seed
_.replaygain_max_clip_safe_gain
# Read through self.__dict__.setdefault("_fx_plans", ...) in player.py.
_._fx_plans
# A worker scan result field (autodj.scan_analysis.ScanResult), read from
# model_dump() when LibraryCatalog.store_analysis stores it.
_.intro_lift_db

# Pytest discovers these registered plugin hooks by name (-p scripts.ci_pytest).
_.pytest_addoption
_.pytest_sessionstart
_.pytest_deselected
_.pytest_testnodedown
_.pytest_sessionfinish
