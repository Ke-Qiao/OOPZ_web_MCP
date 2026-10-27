import unittest
from unittest.mock import patch

import httpx
import numpy as np
from pydantic import ValidationError

from oopz_mcp.backends import LiveBackend
from oopz_mcp.config import ASRConfig, AudioConfig, Limits, Profile, Reply, TTSConfig


def live_profile():
    return Profile(
        backend="live",
        audio=AudioConfig(rx_device="TEST_RX", tx_device="TEST_TX"),
        asr=ASRConfig(),
        tts=TTSConfig(
            endpoint="https://tts.invalid/tts",
            api_revision="fixture-revision",
            model_id="fixture-not-real-weights",
            ref_audio_path="server-side-reference.wav",
        ),
    )


class ValidationTests(unittest.TestCase):
    def test_same_audio_device_is_rejected(self):
        with self.assertRaises(ValidationError):
            AudioConfig(rx_device="RX", tx_device="rx")

    def test_remote_plain_http_and_embedded_secrets_are_rejected(self):
        for endpoint in (
            "http://tts.invalid/tts",
            "https://user:secret@tts.invalid/tts",
            "https://tts.invalid/tts?token=secret",
        ):
            with self.assertRaises(ValidationError):
                TTSConfig(
                    endpoint=endpoint,
                    api_revision="fixture",
                    model_id="fixture",
                    ref_audio_path="reference.wav",
                )

    def test_unknown_keys_and_whitespace_reply_are_rejected(self):
        with self.assertRaises(ValidationError):
            Profile.model_validate({"backend": "mock", "unexpected": True})
        with self.assertRaises(ValidationError):
            Reply(reply_id="a", based_on_input_revision=0, text="   ")

    def test_sample_rate_mismatch_is_rejected(self):
        values = live_profile().model_dump()
        values["tts"]["sample_rate"] = 24000
        with self.assertRaises(ValidationError):
            Profile.model_validate(values)

    def test_xfyun_rejects_non_16khz_input(self):
        values = live_profile().model_dump()
        values["audio"]["rx_sample_rate"] = 48000
        with self.assertRaises(ValidationError):
            Profile.model_validate(values)


class StereoRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_mode_capture_only_queues_audio_without_pausing_output(self):
        backend = LiveBackend(live_profile(), Limits())
        backend.playback.begin()
        with (
            patch.object(backend, "np", np, create=True),
            patch.object(backend, "listening", object()),
        ):
            backend._capture(np.full((320, 1), 0.2, dtype="float32"), 320, None, False)
        pcm, voiced, _ = backend.rx.get_nowait()
        self.assertTrue(voiced)
        np.testing.assert_array_equal(pcm, np.full(320, 0.2, dtype="float32"))
        self.assertFalse(backend.playback.speech_pending)
        self.assertTrue(backend.playback.active)
        self.assertFalse(backend.callback_error)

    async def test_stereo_rx_includes_right_channel_in_mono_asr_input(self):
        profile = live_profile()
        assert profile.audio is not None
        profile.audio.rx_channels = 2
        backend = LiveBackend(profile, Limits())
        samples = np.zeros((320, 2), dtype="float32")
        samples[:, 1] = 0.5
        with patch.object(backend, "np", np, create=True):
            backend._capture(samples, 320, None, False)
        mono, voiced, _ = backend.rx.get_nowait()
        np.testing.assert_array_equal(mono, np.full(320, 0.25, dtype="float32"))
        self.assertTrue(voiced)

    async def test_mono_tts_is_duplicated_to_both_tx_channels_and_stop_still_mutes(self):
        profile = live_profile()
        assert profile.audio is not None
        profile.audio.tx_channels = 2
        backend = LiveBackend(profile, Limits())
        epoch = backend.playback.begin()
        mono = np.arange(640, dtype="<i2")
        await backend.playback.enqueue(epoch, mono.tobytes())
        output = bytearray(2560)
        with patch.object(backend, "np", np, create=True):
            backend._render(output, 640, None, False)
        stereo = np.frombuffer(output, dtype="<i2").reshape(-1, 2)
        np.testing.assert_array_equal(stereo[:, 0], mono)
        np.testing.assert_array_equal(stereo[:, 1], mono)
        self.assertEqual(backend.playback.local_samples, 640)
        backend.playback.stop("fixture_stop")
        with patch.object(backend, "np", np, create=True):
            backend._render(output, 640, None, False)
        self.assertEqual(output, bytes(2560))


class TTSTests(unittest.IsolatedAsyncioTestCase):
    async def test_pcm_stream_is_framed_and_reconstructed_without_audio_devices(self):
        backend = LiveBackend(live_profile(), Limits())
        pcm = b"\x01\x00" * 1001
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "audio/raw"}, content=pcm)

        backend.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            chunks = [chunk async for chunk in backend.synthesize("不要截断完整回答")]
            self.assertEqual(b"".join(chunks), pcm)
            self.assertTrue(all(len(chunk) <= backend.playback.frame_bytes for chunk in chunks))
            self.assertEqual(len(seen), 1)
            self.assertNotIn(b'"gpt_streaming"', seen[0].content)
            self.assertIn(b'"streaming_mode":3', seen[0].content)
        finally:
            await backend.client.aclose()

    async def test_bad_media_empty_or_truncated_pcm_are_rejected(self):
        for media, body in (
            ("application/json", b"{}"),
            ("audio/raw", b""),
            ("audio/raw", b"\x01"),
        ):
            backend = LiveBackend(live_profile(), Limits())
            response = httpx.Response(200, headers={"content-type": media}, content=body)
            backend.client = httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request, response=response: response)
            )
            try:
                with self.assertRaises(ValueError):
                    _ = [chunk async for chunk in backend.synthesize("测试夹具")]
            finally:
                await backend.client.aclose()

    async def test_tts_network_failure_is_not_fabricated_as_audio(self):
        backend = LiveBackend(live_profile(), Limits())

        def fail(request):
            raise httpx.ConnectError("fixture disconnect", request=request)

        backend.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
        try:
            with self.assertRaises(httpx.ConnectError):
                _ = [chunk async for chunk in backend.synthesize("测试夹具")]
        finally:
            await backend.client.aclose()


if __name__ == "__main__":
    unittest.main()
