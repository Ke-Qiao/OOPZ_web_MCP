import contextlib
import io
import json
import os
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from oopz_mcp.config import AudioConfig
from oopz_mcp.speaker_admin import main, read_enrollment_wav, record_enrollment
from oopz_mcp.speech_models import (
    CamPlus,
    SmartTurn,
    SpeakerRegistry,
    SpeechModelError,
    _checked_model,
)

FINGERPRINT = "a" * 64


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "registry.json"
        self.registry = SpeakerRegistry(self.path, FINGERPRINT, 3).load()

    def add(self, speaker_id, vector):
        self.registry.enroll(speaker_id, speaker_id, [np.array(vector)] * 3)

    def test_two_registered_people_unknown_and_ambiguous_are_distinct(self):
        self.add("A", [1, 0, 0])
        self.add("B", [0, 1, 0])
        for speaker_id, vector in (("A", [1, 0, 0]), ("B", [0, 1, 0])):
            match = self.registry.identify(np.array(vector), 0.65, 0.1)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match.speaker_id, speaker_id)
        self.assertIsNone(self.registry.identify(np.array([0, 0, 1]), 0.65, 0.1))
        self.assertIsNone(self.registry.identify(np.array([1, 1, 0]), 0.65, 0.1))

    def test_private_save_reload_and_no_silent_overwrite(self):
        self.add("A", [1, 0, 0])
        self.registry.save()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)
        reloaded = SpeakerRegistry(self.path, FINGERPRINT, 3).load()
        self.assertEqual(reloaded.list(), [{"speaker_id": "A", "name": "A"}])
        with self.assertRaisesRegex(SpeechModelError, "already_registered"):
            self.add("A", [0, 1, 0])
        self.assertTrue(reloaded.remove("A"))
        reloaded.save()
        with self.assertRaisesRegex(SpeechModelError, "changed_reload"):
            self.registry.save()
        self.assertEqual(SpeakerRegistry(self.path, FINGERPRINT, 3).load().list(), [])

    def test_model_change_and_corrupt_vectors_fail_closed(self):
        self.add("A", [1, 0, 0])
        self.registry.save()
        with self.assertRaisesRegex(SpeechModelError, "reenrollment"):
            SpeakerRegistry(self.path, "b" * 64, 3).load()
        original = json.loads(self.path.read_text())
        for vector in (
            [float("nan"), 0, 0],
            [0, 0, 0],
            [1, 0],
            [2, 0, 0],
            ["1", "0", "0"],
            [True, False, False],
        ):
            original["speakers"][0]["vector"] = vector
            self.path.write_text(json.dumps(original))
            with self.assertRaises(SpeechModelError):
                SpeakerRegistry(self.path, FINGERPRINT, 3).load()

    def test_interrupted_replace_preserves_saved_registry_and_cleans_temporary_file(self):
        self.add("A", [1, 0, 0])
        self.registry.save()
        original = self.path.read_bytes()
        self.registry.remove("A")
        with patch("oopz_mcp.speech_models.os.replace", side_effect=OSError("private path")):
            with self.assertRaisesRegex(SpeechModelError, "^speaker_registry_write_failed$"):
                self.registry.save()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_bad_identity_inconsistent_samples_and_unsafe_storage_are_rejected(self):
        with self.assertRaises(SpeechModelError):
            self.add("../A", [1, 0, 0])
        with self.assertRaisesRegex(SpeechModelError, "inconsistent"):
            self.registry.enroll("A", "A", list(np.eye(3)))
        self.add("A", [1, 0, 0])
        self.path.parent.mkdir(mode=0o755)
        self.path.parent.chmod(0o755)
        with self.assertRaisesRegex(SpeechModelError, "private_storage"):
            self.registry.save()
        self.path.parent.chmod(0o700)
        self.registry.save()
        target = self.path.with_name("real.json")
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(SpeechModelError, "private_storage"):
            self.registry.load()


class ModelBoundaryTests(unittest.TestCase):
    def test_checksum_error_does_not_expose_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-model.onnx"
            path.write_bytes(b"fixture")
            with self.assertRaisesRegex(SpeechModelError, "^model_checksum_mismatch$"):
                _checked_model(str(path), FINGERPRINT)

    def test_camplus_rejects_bad_pcm_before_model_inference(self):
        model = CamPlus.__new__(CamPlus)
        for pcm in (
            np.zeros(16000, dtype=np.float64),
            np.zeros((16000, 2), dtype=np.float32),
            np.array([np.nan], dtype=np.float32),
            np.array([1.1], dtype=np.float32),
            np.zeros(240001, dtype=np.float32),
        ):
            with self.assertRaises(SpeechModelError):
                model.embedding(pcm)

    def test_smart_turn_uses_last_eight_seconds_left_padding_and_finite_probability(self):
        model = SmartTurn.__new__(SmartTurn)
        seen = []

        def extract(pcm, **kwargs):
            seen.append(pcm)
            self.assertEqual(kwargs["sampling_rate"], 16000)
            self.assertTrue(kwargs["do_normalize"])
            return SimpleNamespace(input_features=np.zeros((1, 80, 800), dtype=np.float32))

        extractor_patch = patch.object(model, "extractor", extract, create=True)
        session_patch = patch.object(
            model,
            "session",
            SimpleNamespace(run=lambda *_: [np.array([[0.8]], dtype=np.float32)]),
            create=True,
        )
        extractor_patch.start()
        session_patch.start()
        self.addCleanup(extractor_patch.stop)
        self.addCleanup(session_patch.stop)
        self.assertAlmostEqual(model.probability(np.ones(16000, dtype=np.float32)), 0.8)
        np.testing.assert_array_equal(seen[-1][:112000], 0)
        np.testing.assert_array_equal(seen[-1][-16000:], 1)
        long = np.linspace(-1, 1, 16000 * 9, dtype=np.float32)
        model.probability(long)
        np.testing.assert_array_equal(seen[-1], long[-128000:])
        for result in (np.array([[np.nan]]), np.array([[1.01]]), np.array([[0.1, 0.9]])):
            with patch.object(
                model, "session", SimpleNamespace(run=lambda *_, result=result: [result])
            ):
                with self.assertRaisesRegex(SpeechModelError, "smart_turn_inference_failed"):
                    model.probability(np.ones(16000, dtype=np.float32))


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = SimpleNamespace(
            registry_path=str(self.root / "private" / "registry.json"), model_sha256=FINGERPRINT
        )
        self.settings = SimpleNamespace(profiles={"oopz": SimpleNamespace(speakers=self.config)})

    def wav(self, index, channels=1, rate=16000, silence=False):
        path = self.root / f"sample{index}.wav"
        t = np.arange(rate * 2) / rate
        pcm = np.sin(2 * np.pi * (200 + index) * t) * (0 if silence else 8000)
        with wave.open(str(path), "wb") as target:
            target.setnchannels(channels)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(np.tile(pcm[:, None], (1, channels)).astype("<i2").tobytes())
        return path

    def test_wav_requires_correct_format_and_useful_signal(self):
        self.assertEqual(read_enrollment_wav(self.wav(0)).dtype, np.float32)
        for path in (self.wav(1, channels=2), self.wav(2, rate=8000), self.wav(3, silence=True)):
            with self.assertRaises(SpeechModelError):
                read_enrollment_wav(path)

    def test_cli_enroll_list_remove_without_loading_models_for_administration(self):
        args = ["--config", "ignored.toml"]
        wav_args = sum((["--wav", str(self.wav(i))] for i in range(3)), [])
        model = SimpleNamespace(
            fingerprint=FINGERPRINT, dimension=3, embedding=lambda _: np.array([1, 0, 0])
        )
        with patch("oopz_mcp.speaker_admin.load_settings", return_value=self.settings):
            output = io.StringIO()
            with patch("oopz_mcp.speaker_admin.CamPlus", return_value=model):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(args + ["enroll", "--speaker-id", "A", "--name", "甲"] + wav_args), 0
                    )
            self.assertNotIn("vector", output.getvalue())
            with patch("oopz_mcp.speaker_admin.CamPlus", side_effect=AssertionError):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(args + ["list"]), 0)
                    self.assertEqual(main(args + ["remove", "--speaker-id", "A"]), 0)
                self.assertIn("甲", output.getvalue())
                self.assertEqual(
                    SpeakerRegistry(self.config.registry_path, FINGERPRINT, 3).load().list(), []
                )

    def test_duplicate_sample_rejection_does_not_load_model_or_write_registry(self):
        path = self.wav(0)
        with patch("oopz_mcp.speaker_admin.load_settings", return_value=self.settings):
            with patch("oopz_mcp.speaker_admin.CamPlus", side_effect=AssertionError):
                output = io.StringIO()
                with contextlib.redirect_stderr(output):
                    result = main(
                        ["--config", "ignored.toml", "enroll", "--speaker-id", "A", "--name", "A"]
                        + ["--wav", str(path)] * 3
                    )
                self.assertEqual(result, 2)
                self.assertIn("distinct_recordings", output.getvalue())
                self.assertFalse(os.path.exists(self.config.registry_path))

    def test_recording_requires_each_enter_uses_only_configured_rx_and_closes_streams(self):
        audio = AudioConfig(rx_device="OOPZ RX", tx_device="OOPZ TX", rx_channels=2)
        actions = []
        captured = np.zeros((128000, 2), dtype=np.float32)
        captured[:, 1] = 0.2

        class Stream:
            def __init__(self, **kwargs):
                actions.append(("open", kwargs))

            def __enter__(self):
                return self

            def read(self, frames):
                actions.append(("read", frames))
                return captured, False

            def __exit__(self, *_):
                actions.append(("close", None))

        device_api = SimpleNamespace(
            query_devices=lambda: [
                {"name": "Default physical microphone", "max_input_channels": 2},
                {"name": "OOPZ RX", "max_input_channels": 2},
            ],
            InputStream=Stream,
        )
        with patch.dict("sys.modules", {"sounddevice": device_api}):
            with patch("builtins.input", side_effect=lambda _: actions.append(("confirm", None))):
                with contextlib.redirect_stdout(io.StringIO()):
                    samples = record_enrollment(audio)
        self.assertEqual([kind for kind, _ in actions], ["confirm", "open", "read", "close"] * 3)
        self.assertEqual(len(samples), 3)
        for kind, kwargs in actions:
            if kind == "open":
                self.assertEqual(kwargs["device"], 1)
                self.assertEqual(kwargs["samplerate"], 16000)
                self.assertEqual(kwargs["channels"], 2)
        np.testing.assert_array_equal(samples[0], np.full(128000, 0.1, dtype=np.float32))

    def test_missing_rx_does_not_fall_back_and_capture_failure_closes_stream(self):
        audio = AudioConfig(rx_device="OOPZ RX", tx_device="OOPZ TX")
        with patch.dict("sys.modules", {"sounddevice": SimpleNamespace(query_devices=lambda: [])}):
            with patch("builtins.input", side_effect=AssertionError):
                with self.assertRaisesRegex(SpeechModelError, "rx_device_missing"):
                    record_enrollment(audio)
        closed = []

        class FailingStream:
            def __init__(self, **_):
                pass

            def __enter__(self):
                return self

            def read(self, _):
                raise OSError("private path")

            def __exit__(self, *_):
                closed.append(True)

        device_api = SimpleNamespace(
            query_devices=lambda: [{"name": "OOPZ RX", "max_input_channels": 1}],
            InputStream=FailingStream,
        )
        with patch.dict("sys.modules", {"sounddevice": device_api}):
            with (
                patch("builtins.input", return_value=""),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(SpeechModelError, "^enrollment_capture_failed$"):
                    record_enrollment(audio)
        self.assertEqual(closed, [True])
        self.assertFalse(Path(self.config.registry_path).exists())


if __name__ == "__main__":
    unittest.main()
