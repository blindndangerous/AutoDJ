"""The rotation tester's routes: start, follow and cancel one dry run (#125).

A run picks thousands of songs, so it never runs on the event loop: it
gets a daemon thread of its own, below normal priority, and pauses between
picks so the audio and the web server keep their place (see
:mod:`autodj.rotation_sim`).  One run at a time for the whole server;
starting a second answers 409.  The page polls ``GET /rotation-test`` for
progress and, once done, the report in JSON and in words.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from autodj.rotation_sim import (
    MAX_HOURS,
    MAX_PICKS,
    Progress,
    SimulationError,
    SimulationRequest,
    SimulationSource,
    report_sections,
    report_text,
    simulate,
    snapshot_player,
)
from autodj.station_routes import StationDep, StationRouter


class RotationTestBody(BaseModel):
    """What to simulate: hours, days or songs (at most one), an optional start and seed.

    ``start`` is a station wall time such as ``2026-10-12T06:00``; one with
    an offset is taken as given.
    """

    model_config = ConfigDict(extra="forbid")

    hours: float | None = None
    days: float | None = None
    picks: StrictInt | None = None
    seed: StrictInt | None = None
    start: datetime | None = None

    @model_validator(mode="after")
    def _one_length(self) -> RotationTestBody:
        given = [name for name in ("hours", "days", "picks") if getattr(self, name) is not None]
        if len(given) > 1:
            raise ValueError("Give hours, days or songs, only one of them")
        for name in ("hours", "days"):
            value = getattr(self, name)
            if value is not None and not math.isfinite(value):
                raise ValueError(f"{name} must be a number")
        return self

    def request(self, timezone: str) -> SimulationRequest:
        """The :class:`SimulationRequest`, with a wall-clock start read in *timezone*."""
        hours = self.days * 24 if self.days is not None else self.hours
        start = None
        if self.start is not None:
            moment = self.start
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=ZoneInfo(timezone))
            try:
                start = moment.timestamp()
            except (OverflowError, OSError, ValueError):
                start = math.nan  # Refused below, in words.
        return SimulationRequest(hours=hours, picks=self.picks, start=start, seed=self.seed)


class RotationTestRun:
    """One run on its own thread: its progress, then its report or its error."""

    def __init__(self, station: str, source: SimulationSource, request: SimulationRequest):
        self.station = station
        self._source = source
        self._request = request
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self.state = "running"
        self.progress = Progress()
        self.report: dict[str, Any] | None = None
        self.text: str | None = None
        self.sections: list[dict[str, Any]] | None = None
        self.error: str | None = None
        self.started_at = time.time()
        self.finished_at: float | None = None
        self.thread = threading.Thread(target=self._run, name="autodj-rotation-test", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _progress(self, progress: Progress) -> None:
        with self._lock:
            self.progress = progress

    def _run(self) -> None:
        from autodj.rotation_sim import lower_thread_priority

        lower_thread_priority()
        try:
            report = simulate(
                self._source,
                self._request,
                progress=self._progress,
                cancelled=self._cancel.is_set,
            )
            text = report_text(report)
            sections = report_sections(report)
        except SimulationError as exc:
            self._finish("failed", error=str(exc))
        except Exception as exc:  # pragma: no cover - the run reports, never raises
            import logging

            logging.getLogger(__name__).exception("The rotation test failed")
            self._finish("failed", error=f"The rotation test failed: {exc}")
        else:
            state = "cancelled" if report.get("stopped_reason") == "cancelled" else "done"
            self._finish(state, report, text, sections)

    def _finish(
        self,
        state: str,
        report: dict[str, Any] | None = None,
        text: str | None = None,
        sections: list[dict[str, Any]] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self.state = state
            self.report = report
            self.text = text
            self.sections = sections
            self.error = error
            self.finished_at = time.time()

    def view(self) -> dict[str, Any]:
        """The run as the page reads it."""
        with self._lock:
            end = self.finished_at or time.time()
            return {
                "state": self.state,
                "station": self.station,
                "progress": {
                    "picks": self.progress.picks,
                    "percent": round(self.progress.fraction * 100),
                },
                "elapsed_seconds": round(end - self.started_at, 1),
                "report": self.report,
                "text": self.text,
                "sections": self.sections,
                "error": self.error,
            }


class RotationTests:
    """The server's one rotation test slot, and the latest run of each station."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, RotationTestRun] = {}
        # Set when the server starts shutting down: no run starts after it.
        self._closing = False

    def start(self, run: RotationTestRun) -> None:
        """Take the slot for *run* and start it; 409 while any station's run is going.

        A run whose thread cannot start gives the slot back (the station's
        previous result returns) and answers 503.
        """
        with self._lock:
            if self._closing:
                raise HTTPException(status_code=503, detail="The server is shutting down")
            busy = next((other for other in self._runs.values() if other.state == "running"), None)
            if busy is not None:
                where = "" if busy.station == run.station else f" for station {busy.station}"
                raise HTTPException(
                    status_code=409, detail=f"A rotation test is already running{where}"
                )
            previous = self._runs.get(run.station)
            self._runs[run.station] = run
        try:
            run.start()
        except Exception as exc:
            with self._lock:
                if previous is None:
                    self._runs.pop(run.station, None)
                else:
                    self._runs[run.station] = previous
            raise HTTPException(
                status_code=503, detail="The rotation test could not start; try again"
            ) from exc

    def shutdown(self, timeout: float) -> bool:
        """Cancel every run and wait up to *timeout* seconds in all; whether they all ended.

        No run starts once this has begun.
        """
        with self._lock:
            self._closing = True
            runs = [run for run in self._runs.values() if run.state == "running"]
        for run in runs:
            run.cancel()
        deadline = time.monotonic() + timeout
        for run in runs:
            run.thread.join(max(0.0, deadline - time.monotonic()))
        return not any(run.thread.is_alive() for run in runs)

    def get(self, station: str) -> RotationTestRun | None:
        with self._lock:
            return self._runs.get(station)


_IDLE = {
    "state": "idle",
    "progress": {"picks": 0, "percent": 0},
    "elapsed_seconds": 0,
    "report": None,
    "text": None,
    "sections": None,
    "error": None,
}


def _tests(request: Request) -> RotationTests:
    state = request.app.state
    tests = getattr(state, "rotation_tests", None)
    if tests is None:
        tests = state.rotation_tests = RotationTests()
    return tests


def station_source(station: Any) -> SimulationSource:
    """What a run copies from a live *station*."""
    library = station.bridge.library
    if library is None:
        raise HTTPException(status_code=409, detail="The rotation test needs the music library")
    schedule = getattr(station, "schedule", None)
    events = list(schedule.events.values()) if schedule is not None else []
    return SimulationSource(
        player=station.player,
        library=library,
        station=station.slug,
        ledger=station.ledger,
        events=events,
        timezone=station.timezone,
        liner_dates=station.paths.liner_dates,
    )


def install_rotation_test(router: StationRouter) -> None:
    """Add the paired rotation test routes; each acts on the station its URL names."""

    @router.get("/rotation-test")
    async def rotation_test_status(station: StationDep, request: Request) -> dict[str, Any]:
        """The station's latest run: progress while it runs, then the report."""
        run = _tests(request).get(station.slug)
        return {**_IDLE, "station": station.slug} if run is None else run.view()

    @router.post("/rotation-test", status_code=202)
    async def rotation_test_start(
        station: StationDep, request: Request, body: RotationTestBody
    ) -> dict[str, Any]:
        """Start a run; 409 while another is running."""
        source = station_source(station)
        try:
            wanted = body.request(source.timezone)
        except SimulationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # Copied now, in a worker thread: the copy may wait a moment on the
        # settings lock, and the event loop never does.
        snapshot = await asyncio.to_thread(snapshot_player, source.player)
        source = replace(source, snapshot=snapshot)
        run = RotationTestRun(station.slug, source, wanted)
        _tests(request).start(run)
        return run.view()

    @router.delete("/rotation-test")
    async def rotation_test_cancel(station: StationDep, request: Request) -> dict[str, Any]:
        """Stop the station's run; it ends with the report of what it picked so far."""
        run = _tests(request).get(station.slug)
        if run is None or run.state != "running":
            raise HTTPException(status_code=409, detail="No rotation test is running")
        run.cancel()
        return run.view()


__all__ = ["MAX_HOURS", "MAX_PICKS", "RotationTestBody", "install_rotation_test"]
