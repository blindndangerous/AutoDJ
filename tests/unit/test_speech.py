"""Speech production outcomes independent of the live player."""

import base64
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from autodj.speech import SpeechService, generated_clip
from autodj.speech_providers import ProviderStore
from tests.unit.test_speech_providers import audio_bytes


class TestSpeech(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.station = self.root / "a" / "speech"
        self.other = self.root / "b" / "speech"
        self.providers = ProviderStore(self.root / "speech_providers.json")
        self.providers.save(
            "local",
            {
                "engine": "chatterbox",
                "name": "Local",
                "url": "http://127.0.0.1:4123/v1/audio/speech",
                "trusted_local": True,
            },
        )
        self.calls = []

        def render(connection, plan, cancel):
            self.calls.append(plan["text"])
            if plan["settings"]["voice"] == "broken":
                raise ValueError("Provider failed")
            return {"audio": audio_bytes(), "duration": 0.1}

        self.service = SpeechService(
            self.providers, lambda: [self.station, self.other], render=render
        )
        self.addCleanup(self.service.shutdown)

    def wait(self):
        self.assertTrue(self.service.wait(5))

    def submit(self, candidates=None, token="first"):
        return self.service.submit(
            self.station,
            "Station ID",
            candidates
            or [
                {"connection": "local", "voice": "good"},
                {"connection": "local", "voice": "broken"},
            ],
            token,
        )

    def test_station_command_compares_caches_and_rejects_changed_retry(self):
        """ADR-0013-05: station command revision scopes cache, retry and generated sets."""
        command = [
            sys.executable,
            "-c",
            "import sys,base64; assert sys.stdin.read()==sys.argv[2]; "
            "open(sys.argv[1],'wb').write(base64.b64decode(sys.argv[3]))",
            "{out}",
            "{text}",
            base64.b64encode(audio_bytes()).decode(),
        ]
        self.service.command = lambda root: command if root == self.station else []
        request = {"connection": "station-command", "voice": ""}
        self.assertIsNone(
            self.service.preflight("Station ID", [request], root=self.station)["candidates"][0][
                "error"
            ]
        )
        self.assertIsNotNone(
            self.service.preflight("Station ID", [request], root=self.other)["candidates"][0][
                "error"
            ]
        )
        self.submit([request, {"connection": "local", "voice": "broken"}])
        self.wait()
        batch = self.service.view(self.station)["batches"]["first"]
        self.assertEqual([c["state"] for c in batch["candidates"]], ["succeeded", "failed"])
        self.assertNotIn("base64", json.dumps(batch))
        self.submit([request], "cached")
        self.wait()
        self.assertTrue(
            self.service.view(self.station)["batches"]["cached"]["candidates"][0]["cached"]
        )
        self.service.preset(self.station, "command", request)
        with (
            patch("autodj.time_checks.spoken_text", lambda now, fmt: "Station ID"),
            patch("autodj.speech.shutil.disk_usage") as disk,
        ):
            disk.return_value.free = 4 * 2**30
            self.service.generate_times(self.station, "command", "12", "command-set")
        self.wait()
        self.service.activate(self.station, "command-set", True)
        self.service.command = lambda root: []
        self.assertTrue(generated_clip(self.station, datetime(2000, 1, 1, 3, 2))[1].is_file())
        self.service.command = lambda root: command if root == self.station else []
        before = self.service.time_inventory(self.station, "command", "12")["fingerprint"]
        command = [sys.executable, "-c", "raise SystemExit(1)", "{out}"]
        self.assertNotEqual(
            before, self.service.time_inventory(self.station, "command", "12")["fingerprint"]
        )
        self.submit([request], "failed")
        self.wait()
        self.assertEqual(
            self.service.view(self.station)["batches"]["failed"]["candidates"][0]["state"], "failed"
        )
        command = [sys.executable, "-c", "raise SystemExit(2)", "{out}"]
        self.service.retry(self.station, "failed", [0])
        self.wait()
        self.assertIn(
            "changed",
            self.service.view(self.station)["batches"]["failed"]["candidates"][0]["error"],
        )

    def test_success_survives_failure_retry_and_restart(self):
        """ADR-0013-04: failed-only retry preserves successful samples."""
        batch = self.submit()
        self.wait()
        result = self.service.view(self.station)["batches"][batch]
        self.assertEqual([c["state"] for c in result["candidates"]], ["succeeded", "failed"])
        self.service.retry(self.station, batch, [1])
        self.wait()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(
            self.service.submit(
                self.station,
                "Station ID",
                [
                    {"connection": "local", "voice": "good"},
                    {"connection": "local", "voice": "broken"},
                ],
                "first",
            ),
            batch,
        )
        self.assertEqual(len(self.calls), 3)

    def test_cache_and_station_isolation(self):
        """ADR-0013-05: effective request reuse is scoped to one station."""
        candidate = [{"connection": "local", "voice": "good"}]
        self.submit(candidate)
        self.wait()
        self.submit(candidate, "again")
        self.wait()
        self.assertEqual(len(self.calls), 1)
        self.service.submit(self.other, "Station ID", candidate, "other")
        self.wait()
        self.assertEqual(len(self.calls), 2)
        self.providers.save("local", {"name": "Changed"})
        self.submit(candidate, "changed")
        self.wait()
        self.assertEqual(len(self.calls), 3)

    def test_duplicate_fresh_candidates_and_retry_retransmissions_spend_once(self):
        candidate = {"connection": "local", "voice": "broken"}
        batch = self.service.submit(
            self.station, "Station ID", [candidate, candidate], "duplicates", fresh=True
        )
        self.wait()
        self.assertEqual(len(self.calls), 1)
        self.service.retry(self.station, batch, [0, 1], attempt="retry-once")
        self.wait()
        self.service.retry(self.station, batch, [0, 1], attempt="retry-once")
        self.wait()
        self.assertEqual(len(self.calls), 2)

    def test_save_keeps_audio_out_of_rotation_and_default_unchanged(self):
        """ADR-0013-06: saving never publishes or changes a voice default."""
        batch = self.submit([{"connection": "local", "voice": "good"}])
        self.wait()
        saved = self.service.save(self.station, batch, 0, "Morning ID")
        self.assertFalse((self.station.parent / "liners").exists())
        self.assertIsNone(self.service.view(self.station)["default_preset"])
        target = self.station.parent / "liners"
        published = self.service.publish(self.station, saved, target)
        self.assertTrue(published.is_file())
        self.assertTrue(published.with_suffix(".json").is_file())
        self.assertEqual(self.service.view(self.station)["saved"][saved]["text"], "Station ID")

    def test_busy_cancel_and_interrupted_restart_never_resubmit(self):
        """ADR-0013-03: admission is bounded and restart never repeats paid work."""
        entered = threading.Event()
        release = threading.Event()

        def slow(connection, plan, cancel):
            entered.set()
            release.wait(3)
            return {"audio": audio_bytes(), "duration": 0.1}

        self.service.render = slow
        self.submit([{"connection": "local", "voice": "good"}])
        self.assertTrue(entered.wait(2))
        with self.assertRaises(ValueError):
            self.submit(token="busy")
        self.service.cancel()
        release.set()
        self.wait()
        self.assertEqual(
            self.service.view(self.station)["batches"]["first"]["candidates"][0]["state"],
            "cancelled",
        )

    def test_presets_and_bulk_inventory_do_not_generate(self):
        """ADR-0013-09: inventory and default selection never spend."""
        candidate = {"connection": "local", "voice": "good"}
        self.service.preset(self.station, "morning", candidate)
        self.service.default(self.station, "morning")
        inventory = self.service.time_inventory(self.station, "morning", "12")
        self.assertEqual(len(inventory["minutes"]), 1440)
        self.assertGreater(inventory["requests"], 700)
        self.assertIsNone(inventory["cost"])
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.service.view(self.other)["default_preset"])

    def test_complete_sets_activate_atomically_and_survive_without_provider(self):
        """ADR-0013-07: complete local sets are independent of credentials and cache."""
        self.service.preset(self.station, "morning", {"connection": "local", "voice": "good"})
        with (
            patch("autodj.time_checks.spoken_text", lambda now, fmt: f"Minute {now.minute % 2}"),
            patch("autodj.speech.shutil.disk_usage") as disk,
        ):
            disk.return_value.free = 4 * 2**30
            self.service.generate_times(self.station, "morning", "24", "day-one")
        self.wait()
        with self.assertRaises(ValueError):
            self.service.activate(self.station, "day-one", False)
        self.service.activate(self.station, "day-one", True)
        selected, clip = generated_clip(self.station, datetime(2000, 1, 1, 3, 2))
        self.assertTrue(selected)
        self.assertTrue(clip.is_file())
        with self.assertRaises(ValueError):
            self.service.activate(self.station, "incomplete", True)
        self.assertEqual(self.service.view(self.station)["active_set"], "day-one")
        self.providers.path.unlink()
        self.assertTrue(generated_clip(self.station, datetime(2000, 1, 1, 3, 2))[1].is_file())
        clip.unlink()
        self.assertEqual(generated_clip(self.station, datetime(2000, 1, 1, 3, 2)), (True, None))

    def test_expired_partial_set_can_be_explicitly_regenerated(self):
        """ADR-0013-05: expired paid audio requires an explicit admission to recover."""
        self.service.preset(self.station, "morning", {"connection": "local", "voice": "good"})
        original = self.service.render

        def fail_one(connection, plan, cancel):
            if plan["text"] == "Minute 1":
                raise ValueError("Unavailable")
            return original(connection, plan, cancel)

        self.service.render = fail_one
        with (
            patch("autodj.time_checks.spoken_text", lambda now, fmt: f"Minute {now.minute % 2}"),
            patch("autodj.speech.shutil.disk_usage") as disk,
        ):
            disk.return_value.free = 4 * 2**30
            self.service.generate_times(self.station, "morning", "24", "partial")
        self.wait()
        self.service.sample(self.station, "partial", 0).unlink()
        for cache in (self.station / "cache").glob("*.wav"):
            cache.unlink()
        self.assertEqual(
            self.service.view(self.station)["batches"]["partial"]["candidates"][0]["state"],
            "expired",
        )
        self.service.render = original
        self.service.retry(self.station, "partial", [0, 1], attempt="recover")
        self.wait()
        self.assertEqual(self.service.view(self.station)["sets"]["partial"]["state"], "complete")

    def test_restart_marks_inflight_work_interrupted_without_generation(self):
        """ADR-0013-03: startup preserves an interrupted request without resubmitting."""
        self.submit([{"connection": "local", "voice": "good"}])
        self.wait()
        path = self.station / "speech.json"
        data = json.loads(path.read_text())
        data["batches"]["first"]["candidates"][0]["state"] = "running"
        path.write_text(json.dumps(data))
        service = SpeechService(self.providers, lambda: [self.station], render=self.service.render)
        self.addCleanup(service.shutdown)
        self.assertEqual(
            service.view(self.station)["batches"]["first"]["candidates"][0]["state"], "interrupted"
        )
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
