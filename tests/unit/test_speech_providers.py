"""Speech protocol contracts; no live provider calls or credentials."""

import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from autodj import speech_providers as providers


def audio_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\x01\x10" * 2205)
    return stream.getvalue()


class TestSpeechProviders(unittest.TestCase):
    def test_deadline_terminates_and_reaps_a_stuck_provider_worker(self):
        """ADR-0013-03: the wall deadline includes name resolution and decoding."""
        real_popen = subprocess.Popen
        children = []

        def stuck(*args, **kwargs):
            child = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with (
            patch.object(providers, "DEADLINE_SECONDS", 0.05),
            patch("subprocess.Popen", side_effect=stuck),
            self.assertRaises(ValueError),
        ):
            providers.synthesize(
                {"engine": "chatterbox", "url": "https://example.com"},
                providers.prepare({"engine": "chatterbox"}, "Hello", {"voice": "v"}),
                threading.Event(),
            )
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(children)
        self.assertIsNotNone(children[0].poll())

    def test_private_credentials_never_appear_in_public_settings(self):
        """ADR-0013-01: saved keys are private and browser views are write-only."""
        with tempfile.TemporaryDirectory() as directory:
            store = providers.ProviderStore(Path(directory) / "speech_providers.json")
            store.save(
                "main",
                {
                    "name": "Main",
                    "engine": "openai",
                    "url": "https://api.openai.com/v1/audio/speech",
                    "api_key": "private-secret",
                },
            )
            self.assertNotIn("private-secret", json.dumps(store.public()))
            self.assertTrue(store.public()[0]["credential_present"])
            if os.name != "nt":
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)
            with patch.dict(os.environ, {"AUTODJ_SPEECH_MAIN_KEY": "environment-secret"}):
                self.assertEqual(store.connection("main")["api_key"], "environment-secret")
                with self.assertRaises(ValueError):
                    store.save("main", {"api_key": "replacement"})

    def test_protocol_requests_and_base64_audio_for_all_engines(self):
        """ADR-0013-02: documented profiles encode text and decode only audio."""
        for engine in providers.ENGINES:
            with self.subTest(engine=engine):
                connection = {
                    "engine": engine,
                    "url": "https://speech.example/speak",
                    "api_key": "private",
                    "revision": "1",
                }
                settings = {"voice": "announcer"}
                if engine in ("openai", "speechify"):
                    settings["model"] = "example"
                plan = providers.prepare(connection, "Hello <everyone>", settings)
                self.assertEqual(plan["text"], "Hello <everyone>")
                self.assertEqual(
                    plan["payload"]["input"],
                    "Hello &lt;everyone&gt;" if engine == "speechify" else "Hello <everyone>",
                )
                self.assertEqual(
                    plan["payload"].get("voice_id", plan["payload"].get("voice")), "announcer"
                )
                raw = audio_bytes()
                if engine == "speechify":
                    raw = json.dumps({"audio_data": base64.b64encode(raw).decode()}).encode()
                result = providers.synthesize(connection, plan, transport=lambda *a, raw=raw: raw)
                self.assertGreater(result["duration"], 0)
                self.assertTrue(result["audio"].startswith(b"RIFF"))

    def test_capabilities_refuse_silent_drops_and_escape_markup(self):
        connection = {"engine": "speechify", "api_key": "private"}
        plan = providers.prepare(
            connection, '<say & "hi">', {"voice": "v", "model": "m", "speed": 1.1}
        )
        self.assertIn("&lt;say &amp;", plan["payload"]["input"])
        self.assertEqual(plan["controls"]["speed"], "applied")
        with self.assertRaises(ValueError):
            providers.prepare(
                {"engine": "fish"}, "Hello", {"voice": "v", "instructions": "Whisper"}
            )
        with self.assertRaises(ValueError):
            providers.prepare(
                {"engine": "openai", "api_key": "key"}, "x" * 4097, {"voice": "v", "model": "tts-1"}
            )

    def test_untrusted_urls_and_network_destinations_are_refused(self):
        """ADR-0013-02: endpoint settings cannot expose credentials to metadata."""
        for url in [
            "file:///etc/passwd",
            "https://user:secret@example.com/speech",
            "https://169.254.169.254/latest",
            "https://100.100.100.200/latest",
            "https://[fd00:ec2::254]/latest",
            "http://example.com/speech",
            "https://example.com/speech#fragment",
        ]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                providers.validate_url(url)
        with self.assertRaises(ValueError):
            providers.safe_addresses("169.254.169.254", 80)
        self.assertTrue(providers.validate_url("http://127.0.0.1:4123/v1/audio/speech", True))
        for address in ("100.100.100.200", "fd00:ec2::254", "::ffff:169.254.169.254"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                providers.validate_url(
                    f"http://[{address}]/" if ":" in address else f"http://{address}/", True
                )

        with (
            patch.object(providers, "safe_addresses", return_value=["1.1.1.1"]),
            patch.object(providers.socket, "create_connection") as connect,
            self.assertRaises(ValueError),
        ):
            providers.request_audio(
                {"url": "http://example.com/speech", "trusted_local": True}, {}, threading.Event()
            )
        connect.assert_not_called()

    def test_owned_worker_posts_audio_without_following_redirects(self):
        """A real local HTTP exchange checks the subprocess and destination boundary."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302 if self.path == "/redirect" else 200)
                self.send_header("Location", "/must-not-follow")
                self.end_headers()
                if self.path != "/redirect":
                    self.wfile.write(audio_bytes())

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = {"engine": "chatterbox", "trusted_local": True, "api_key": "fixture-key"}
            plan = providers.prepare(connection, "Station ID", {"voice": "v"})
            connection["url"] = f"http://127.0.0.1:{server.server_port}/audio"
            result = providers.synthesize(connection, plan, threading.Event())
            self.assertGreater(result["duration"], 0)
            connection["url"] = f"http://127.0.0.1:{server.server_port}/redirect"
            with self.assertRaises(ValueError):
                providers.synthesize(connection, plan, threading.Event())
            self.assertEqual(
                received, [("/audio", "Bearer fixture-key"), ("/redirect", "Bearer fixture-key")]
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_silence_malformed_json_and_oversized_audio_fail_safely(self):
        plan = providers.prepare(
            {"engine": "speechify", "api_key": "private"}, "Hi", {"voice": "v", "model": "m"}
        )
        for raw in [b"not json", b'{"audio_data":"invalid"}', b'{"audio_url":"https://other"}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                providers.synthesize(
                    {"engine": "speechify"}, plan, transport=lambda *a, raw=raw: raw
                )

    def test_compressed_audio_cannot_bypass_decoded_allocation_limit(self):
        import numpy as np
        import soundfile as sf

        packed = io.BytesIO()
        sf.write(packed, np.full((2205, 2), 0.5), 22050, format="FLAC")
        self.assertLess(len(packed.getvalue()), 10000)
        with patch.object(providers, "MAX_BYTES", 10000), self.assertRaises(ValueError):
            providers.checked_audio(packed.getvalue())


if __name__ == "__main__":
    unittest.main()
