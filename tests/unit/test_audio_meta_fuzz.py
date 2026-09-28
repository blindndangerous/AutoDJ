"""Hypothesis property tests for the LRC sidecar parser.

Any exception from ``parse_lrc`` counts as a finding.  The example budget
stays small so the suite runs on every push.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from autodj.audio_meta import parse_lrc


@settings(max_examples=50, database=None)
@given(text=st.text(max_size=4096))
def test_parse_lrc_never_crashes_on_arbitrary_text(text: str) -> None:
    """``parse_lrc`` must return a list (possibly empty) for any input."""
    out = parse_lrc(text)
    assert isinstance(out, list)


@settings(max_examples=50, database=None)
@given(
    timestamps=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=999),
            st.integers(min_value=0, max_value=99),
            st.integers(min_value=0, max_value=999),
        ),
        max_size=64,
    ),
    payload=st.text(max_size=128),
)
def test_parse_lrc_handles_synthetic_lrc(
    timestamps: list[tuple[int, int, int]],
    payload: str,
) -> None:
    """Synthesise plausible LRC and confirm no crash and bounded output."""
    lines = []
    for m, s, ms in timestamps:
        lines.append(f"[{m:02d}:{s:02d}.{ms:03d}]{payload}")
    out = parse_lrc("\n".join(lines))
    assert isinstance(out, list)
    assert len(out) <= len(timestamps)
