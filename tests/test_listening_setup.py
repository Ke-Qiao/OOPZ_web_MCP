import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError
from test_adapters import live_profile

from oopz_mcp.backends import LiveBackend
from oopz_mcp.config import Limits, Profile, load_settings


class ListeningSetupTests(unittest.TestCase):
    def values(self):
        values = live_profile().model_dump()
        values["speakers"] = {"model_path": "cam.onnx", "model_sha256": "a" * 64}
        values["turn"] = {"model_path": "turn.onnx", "model_sha256": "b" * 64}
        return values

    def test_models_must_be_enabled_together_on_live_profiles(self):
        values = self.values()
        Profile.model_validate(values)
        for key in ("speakers", "turn"):
            partial = dict(values)
            partial[key] = None
            with self.assertRaises(ValidationError):
                Profile.model_validate(partial)

    def test_paths_resolve_relative_to_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.toml"
            path.write_text(
                '''
[profiles.oopz]
backend = "live"
[profiles.oopz.audio]
rx_device = "rx"
tx_device = "tx"
[profiles.oopz.asr]
[profiles.oopz.tts]
endpoint = "http://127.0.0.1:9880/tts"
api_revision = "fixture"
model_id = "fixture"
ref_audio_path = "fixture.wav"
[profiles.oopz.speakers]
model_path = "cam.onnx"
model_sha256 = "'''
                + "a" * 64
                + '''"
[profiles.oopz.turn]
model_path = "turn.onnx"
model_sha256 = "'''
                + "b" * 64
                + """"
"""
            )
            profile = load_settings(path).profiles["oopz"]
            assert profile.speakers and profile.turn
            self.assertEqual(profile.speakers.model_path, str(path.parent.resolve() / "cam.onnx"))
            self.assertEqual(profile.turn.model_path, str(path.parent.resolve() / "turn.onnx"))
            self.assertEqual(
                profile.speakers.registry_path,
                str(path.parent.resolve() / ".runtime/speakers/registry.json"),
            )

    def test_empty_registry_rejects_before_device_discovery_or_stream_open(self):
        with tempfile.TemporaryDirectory() as folder:
            values = self.values()
            values["speakers"]["registry_path"] = str(Path(folder) / "private/registry.json")
            backend = LiveBackend(Profile.model_validate(values), Limits())
            with (
                patch(
                    "oopz_mcp.listening.CamPlus",
                    return_value=SimpleNamespace(fingerprint="a" * 64, dimension=192),
                ),
                patch("oopz_mcp.listening.SmartTurn") as detector,
                patch("sounddevice.query_devices") as devices,
                patch("sounddevice.InputStream") as capture,
                patch("sounddevice.RawOutputStream") as output,
            ):
                with self.assertRaisesRegex(ValueError, "speaker_registry_empty"):
                    backend._prepare()
                for mocked in (detector, devices, capture, output):
                    mocked.assert_not_called()

    def test_mock_server_does_not_import_optional_model_or_audio_dependencies(self):
        code = """
import importlib.abc
import sys
class BlockOptional(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'numpy', 'sounddevice', 'sherpa_onnx', 'onnxruntime', 'transformers'}:
            raise ImportError('optional dependency must not be imported: ' + fullname)
sys.meta_path.insert(0, BlockOptional())
from oopz_mcp.server import create_server
from oopz_mcp.config import Settings
create_server(Settings())
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
