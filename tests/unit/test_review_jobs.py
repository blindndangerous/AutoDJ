"""The render slot and immutable review artifacts survive failures independently."""

import json
import threading
import time

import pytest


def write_audio(stage, cancelled):
    assert not cancelled()
    (stage / "review.wav").write_bytes(b"wave")
    (stage / "review.mp3").write_bytes(b"mp3")
    return {"items": [{"title": "Review"}]}


def wait(jobs, root, identity):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        state = jobs.status(root, identity)
        if state["state"] != "running":
            return state
        time.sleep(0.005)
    pytest.fail("review worker did not finish")


def test_completed_results_are_immutable_and_survive_restart(tmp_path):
    """ADR-0014-04: completion is immutable and survives service restart."""
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs()
    job = jobs.submit(tmp_path, "original", write_audio)
    assert wait(jobs, tmp_path, job["id"])["state"] == "done"
    assert jobs.shutdown()
    restored = ReviewJobs()
    result = restored.results(tmp_path, "edited")[0]
    assert result["id"] == job["id"]
    assert result["stale"] is True
    assert result["metadata"]["items"][0]["title"] == "Review"
    assert restored.audio_path(tmp_path, job["id"], "wav").read_bytes() == b"wave"
    assert restored.audio_path(tmp_path, job["id"], "mp3").read_bytes() == b"mp3"
    restored.delete(tmp_path, job["id"])
    assert restored.results(tmp_path) == []


def test_one_slot_cancellation_cleanup_and_retry_across_stations(tmp_path):
    from autodj.review_jobs import ReviewJobs

    entered = threading.Event()

    def slow(stage, cancelled):
        (stage / "partial").write_bytes(b"unfinished")
        entered.set()
        while not cancelled():
            time.sleep(0.001)
        return {}

    jobs = ReviewJobs()
    root = tmp_path / "first"
    job = jobs.submit(root, "one", slow)
    assert entered.wait(1)
    with pytest.raises(RuntimeError, match="already running"):
        jobs.submit(tmp_path / "second", "two", write_audio)
    with pytest.raises(FileNotFoundError):
        jobs.cancel(tmp_path / "second", job["id"])
    jobs.cancel(root, job["id"])
    assert wait(jobs, root, job["id"])["state"] == "cancelled"
    assert not list(root.glob(".staging-*"))
    following = jobs.submit(root, "retry", write_audio)
    assert wait(jobs, root, following["id"])["state"] == "done"
    assert jobs.shutdown()


@pytest.mark.parametrize("failure", ["decode", "tts", "encode", "disk"])
def test_failed_render_preserves_previous_and_releases_capacity(tmp_path, failure):
    """ADR-0014-04: failures retain prior results and release the worker slot."""
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs()
    old = jobs.submit(tmp_path, "old", write_audio)
    assert wait(jobs, tmp_path, old["id"])["state"] == "done"

    def broken(stage, cancelled):
        (stage / "partial").write_bytes(b"unfinished")
        raise OSError(failure)

    failed = jobs.submit(tmp_path, "new", broken)
    status = wait(jobs, tmp_path, failed["id"])
    assert status["state"] == "failed"
    assert status["error"]
    assert [item["id"] for item in jobs.results(tmp_path)] == [old["id"]]
    assert not list(tmp_path.glob(".staging-*"))
    retry = jobs.submit(tmp_path, "retry", write_audio)
    assert wait(jobs, tmp_path, retry["id"])["state"] == "done"
    assert jobs.shutdown()


def test_quota_refuses_without_evicting_old_results(tmp_path):
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs(max_results=1)
    first = jobs.submit(tmp_path, "one", write_audio)
    assert wait(jobs, tmp_path, first["id"])["state"] == "done"
    with pytest.raises(ValueError, match="limit"):
        jobs.submit(tmp_path, "two", write_audio)
    assert jobs.audio_path(tmp_path, first["id"], "wav").read_bytes() == b"wave"
    assert jobs.shutdown()
    jobs = ReviewJobs(max_bytes=1)
    with pytest.raises(ValueError, match="limit"):
        jobs.submit(tmp_path, "two", write_audio)
    assert jobs.shutdown()


def test_publish_checks_new_bytes_and_required_files(tmp_path):
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs(max_bytes=1)
    first = jobs.submit(tmp_path, "one", write_audio)
    assert wait(jobs, tmp_path, first["id"])["state"] == "failed"
    assert jobs.results(tmp_path) == []
    assert not list(tmp_path.glob(".staging-*"))
    jobs.shutdown()
    jobs = ReviewJobs()
    missing = jobs.submit(tmp_path, "missing", lambda stage, cancelled: {})
    assert wait(jobs, tmp_path, missing["id"])["state"] == "failed"
    assert jobs.shutdown()


def test_recovery_marks_interrupted_job_failed_and_removes_only_its_staging(tmp_path):
    from autodj.review_jobs import ReviewJobs

    identity = "a" * 32
    stage = tmp_path / f".staging-{identity}"
    stage.mkdir()
    (stage / "partial").write_bytes(b"partial")
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (tmp_path / ".job.json").write_text(json.dumps({"id": identity, "state": "running"}))
    jobs = ReviewJobs()
    assert jobs.status(tmp_path, identity)["state"] == "failed"
    assert not stage.exists()
    assert unrelated.exists()
    retry = jobs.submit(tmp_path, "recovered", write_audio)
    assert wait(jobs, tmp_path, retry["id"])["state"] == "done"
    assert jobs.shutdown()


def test_deadline_and_shutdown_do_not_release_unfinished_owner(tmp_path):
    from autodj.review_jobs import ReviewJobs

    entered, release = threading.Event(), threading.Event()

    def uncooperative(stage, cancelled):
        entered.set()
        release.wait(2)
        return write_audio(stage, lambda: False)

    jobs = ReviewJobs(max_seconds=0.01)
    job = jobs.submit(tmp_path, "one", uncooperative)
    assert entered.wait(1)
    time.sleep(0.02)
    assert not jobs.shutdown(timeout=0.01)
    with pytest.raises(RuntimeError, match="shutting down"):
        jobs.submit(tmp_path, "two", write_audio)
    release.set()
    assert jobs.shutdown(timeout=1)
    assert jobs.status(tmp_path, job["id"])["state"] in {"cancelled", "failed"}
    assert jobs.results(tmp_path) == []


@pytest.mark.parametrize("identity", ["../outside", "/tmp/outside", "A" * 32, "short"])
def test_ids_cannot_address_paths(tmp_path, identity):
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs()
    for action in (
        lambda: jobs.status(tmp_path, identity),
        lambda: jobs.cancel(tmp_path, identity),
        lambda: jobs.result(tmp_path, identity),
        lambda: jobs.audio_path(tmp_path, identity, "wav"),
        lambda: jobs.delete(tmp_path, identity),
    ):
        with pytest.raises(ValueError, match="ID"):
            action()
    assert not list(tmp_path.iterdir())
    assert jobs.shutdown()


def test_symlink_result_and_audio_are_never_served(tmp_path):
    from autodj.review_jobs import ReviewJobs

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}")
    root = tmp_path / "reviews"
    root.mkdir()
    identity = "b" * 32
    (root / identity).symlink_to(outside, target_is_directory=True)
    jobs = ReviewJobs()
    assert jobs.results(root) == []
    with pytest.raises(FileNotFoundError):
        jobs.audio_path(root, identity, "wav")
    with pytest.raises(FileNotFoundError):
        jobs.delete(root, identity)
    assert outside.exists()
    assert jobs.shutdown()


def test_storage_limit_signals_cancel_before_renderer_finishes(tmp_path):
    from autodj.review_jobs import ReviewJobs

    observed = []

    def oversized(stage, cancelled):
        (stage / "review.wav").write_bytes(b"too large")
        observed.append(cancelled())
        return {}

    jobs = ReviewJobs(max_bytes=2)
    job = jobs.submit(tmp_path, "large", oversized)
    assert wait(jobs, tmp_path, job["id"])["state"] == "failed"
    assert observed == [True]
    assert not list(tmp_path.glob(".staging-*"))
    assert jobs.shutdown()


def test_cooperative_wall_limit_fails_and_allows_retry(tmp_path):
    from autodj.review_jobs import ReviewJobs

    def until_deadline(stage, cancelled):
        while not cancelled():
            time.sleep(0.001)
        return {}

    jobs = ReviewJobs(max_seconds=0.02)
    timed = jobs.submit(tmp_path, "timeout", until_deadline)
    assert "timed out" in wait(jobs, tmp_path, timed["id"])["error"]
    jobs.max_seconds = 5
    retry = jobs.submit(tmp_path, "retry", write_audio)
    assert wait(jobs, tmp_path, retry["id"])["state"] == "done"
    assert jobs.shutdown()


def test_manifest_disk_failure_cleans_partial_and_preserves_old_result(tmp_path, monkeypatch):
    from autodj import review_jobs

    jobs = review_jobs.ReviewJobs()
    previous = jobs.submit(tmp_path, "old", write_audio)
    assert wait(jobs, tmp_path, previous["id"])["state"] == "done"
    original_write = review_jobs.atomic_write

    def full_disk(path, data, **kwargs):
        if path.name == "manifest.json":
            raise OSError("disk full")
        return original_write(path, data, **kwargs)

    monkeypatch.setattr(review_jobs, "atomic_write", full_disk)
    broken = jobs.submit(tmp_path, "new", write_audio)
    assert wait(jobs, tmp_path, broken["id"])["state"] == "failed"
    assert jobs.audio_path(tmp_path, previous["id"], "wav").read_bytes() == b"wave"
    assert not list(tmp_path.glob(".staging-*"))
    assert jobs.shutdown()


def test_delete_after_restart_removes_persisted_status(tmp_path):
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs()
    job = jobs.submit(tmp_path, "old", write_audio)
    assert wait(jobs, tmp_path, job["id"])["state"] == "done"
    assert jobs.shutdown()
    restarted = ReviewJobs()
    restarted.delete(tmp_path, job["id"])
    final = ReviewJobs()
    with pytest.raises(FileNotFoundError):
        final.status(tmp_path, job["id"])
    assert final.shutdown()


def test_current_is_station_scoped_and_restored(tmp_path):
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs()
    assert jobs.current(tmp_path) is None
    job = jobs.submit(tmp_path, "one", write_audio)
    assert wait(jobs, tmp_path, job["id"])["state"] == "done"
    assert jobs.current(tmp_path)["id"] == job["id"]
    assert jobs.current(tmp_path / "other") is None
    assert jobs.shutdown()
    assert ReviewJobs().current(tmp_path)["state"] == "done"


def test_publication_sync_failure_rolls_back_new_snapshot(tmp_path, monkeypatch):
    from autodj import review_jobs

    jobs = review_jobs.ReviewJobs()
    old = jobs.submit(tmp_path, "old", write_audio)
    assert wait(jobs, tmp_path, old["id"])["state"] == "done"

    def failed_sync(root):
        raise OSError("directory sync failed")

    monkeypatch.setattr(review_jobs, "fsync_directory", failed_sync)
    new = jobs.submit(tmp_path, "new", write_audio)
    assert wait(jobs, tmp_path, new["id"])["state"] == "failed"
    assert [result["id"] for result in jobs.results(tmp_path)] == [old["id"]]
    assert jobs.shutdown()


def test_unexpected_errors_do_not_expose_provider_secrets(tmp_path):
    from autodj.review_jobs import ReviewJobs

    def sensitive_error(stage, cancelled):
        raise OSError("encoder --api-key SECRET /private/path")

    jobs = ReviewJobs()
    job = jobs.submit(tmp_path, "one", sensitive_error)
    status = wait(jobs, tmp_path, job["id"])
    assert status["state"] == "failed"
    assert "SECRET" not in status["error"]
    assert "/private/path" not in status["error"]
    assert "SECRET" not in (tmp_path / ".job.json").read_text()
    assert jobs.shutdown()


def test_scratch_budget_is_separate_from_published_artifact_quota(tmp_path):
    from autodj.review_jobs import ReviewJobs

    jobs = ReviewJobs(max_bytes=1024, max_scratch_bytes=4096)

    def with_scratch(stage, cancelled):
        path = stage / "scratch.pcm"
        path.write_bytes(bytes(2048))
        assert not cancelled()
        path.unlink()
        return write_audio(stage, cancelled)

    job = jobs.submit(tmp_path, "scratch", with_scratch)
    assert wait(jobs, tmp_path, job["id"])["state"] == "done"
    assert jobs.shutdown()


def test_audio_publication_flushes_writable_handles_before_rename(tmp_path, monkeypatch):
    """Windows requires writable handles for FlushFileBuffers behind os.fsync."""
    import os
    import stat

    from autodj import review_jobs

    fsync = os.fsync
    flushed_files = []

    def require_writable(descriptor):
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            # A zero-byte write checks handle access without changing the file.
            # POSIX otherwise permits fsync on read-only files, unlike Windows.
            os.write(descriptor, b"")
            flushed_files.append(os.fstat(descriptor).st_size)
        fsync(descriptor)

    monkeypatch.setattr(review_jobs.os, "fsync", require_writable)
    jobs = review_jobs.ReviewJobs()
    job = jobs.submit(tmp_path, "portable", write_audio)
    assert wait(jobs, tmp_path, job["id"])["state"] == "done"
    assert 4 in flushed_files and 3 in flushed_files
    assert jobs.audio_path(tmp_path, job["id"], "wav").read_bytes() == b"wave"
    assert jobs.audio_path(tmp_path, job["id"], "mp3").read_bytes() == b"mp3"
    assert not list(tmp_path.glob(".staging-*"))
    assert jobs.shutdown()
