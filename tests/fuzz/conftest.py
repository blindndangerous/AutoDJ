"""Hypothesis profile registration for the nightly fuzz suite.

Loaded by pytest because this module sits inside ``tests/fuzz/``.  The
``nightly`` profile bumps Hypothesis' example budget so the scheduled
fuzz job exercises far more inputs than a per-PR run.
"""

from hypothesis import HealthCheck, settings

settings.register_profile(
    "nightly",
    max_examples=2000,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
settings.load_profile("nightly")
