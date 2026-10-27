import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from oopz_mcp.config import ASRConfig, AudioConfig, Profile, SpeakerConfig, TTSConfig, TurnConfig
from oopz_mcp.listening import ListeningController
from oopz_mcp.playback import Playback


class Encoder:
    def embedding(self, pcm):
        voice = pcm[pcm != 0]
        return float(voice.mean()) if voice.size else 0.0


class Registry:
    def identify(self, value, *_):
        if abs(value - 0.1) < 0.01:
            return SimpleNamespace(speaker_id="A")
        if abs(value - 0.2) < 0.01:
            return SimpleNamespace(speaker_id="B")
        return None

    def list(self):
        return [{"speaker_id": "A", "name": "A"}, {"speaker_id": "B", "name": "B"}]


class Detector:
    def __init__(self, value=0.9):
        self.value = value
        self.calls = 0

    def probability(self, _):
        self.calls += 1
        return self.value


class BlockingDetector(Detector):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def probability(self, _):
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait(3)
        return self.value


class ListeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.controllers = []

    async def asyncTearDown(self):
        for controller in self.controllers:
            await controller.close()

    def make(self, detector=None):
        profile = Profile(
            backend="live",
            audio=AudioConfig(rx_device="fixture-rx", tx_device="fixture-tx"),
            asr=ASRConfig(final_timeout_seconds=2),
            tts=TTSConfig(
                endpoint="http://127.0.0.1:9880/tts",
                api_revision="fixture",
                model_id="fixture",
                ref_audio_path="fixture.wav",
            ),
            speakers=SpeakerConfig(model_path="fixture", model_sha256="a" * 64, window_ms=1000),
            turn=TurnConfig(
                model_path="fixture", model_sha256="b" * 64, inference_timeout_seconds=1
            ),
        )
        playback = Playback(32000, 20, 80)
        events = []
        controller = ListeningController(
            profile, playback, events.append, Encoder(), Registry(), detector or Detector()
        )
        controller.start()
        self.controllers.append(controller)
        return controller, playback, events

    def frames(self, controller, start, duration, speaker=0.0):
        for at in range(start, start + duration, 20):
            controller.feed_frame(
                np.full(320, speaker, dtype=np.float32),
                bool(speaker),
                100.0 + (at + 20) / 1000,
                at,
                at + 20,
            )

    async def until(self, predicate):
        async with asyncio.timeout(1.5):
            while not predicate():  # noqa: ASYNC110 - bounded inspection of a worker-thread fixture
                await asyncio.sleep(0.002)

    async def drained(self, controller):
        await self.until(lambda: controller.queue.empty() or controller.failed)
        await asyncio.sleep(0.02)

    def final(self, controller, start, end, text):
        controller.feed_final({"start_ms": start, "end_ms": end, "text": text})

    async def test_explicitly_disabled_identity_keeps_smart_turn_and_accepts_short_tails(self):
        controller, playback, events = self.make()
        self.assertTrue(controller.speakers.enabled)
        controller.speakers.enabled = False
        controller.encoder = controller.registry = None
        playback.begin()
        self.frames(controller, 0, 300, 0.3)
        await self.drained(controller)
        self.assertTrue(playback.speech_pending)
        self.final(controller, 0, 300, "请停一下")
        self.frames(controller, 300, 500)
        await self.drained(controller)
        self.assertEqual(
            [e["type"] for e in events], ["speaker_activity", "transcript", "speech_end"]
        )
        self.assertTrue(all(e["speaker_id"] == "unverified" for e in events))
        self.assertEqual(controller.detector.calls, 1)
        self.assertEqual(controller.filtered_segments, 0)
        self.assertFalse(controller.snapshot()["speaker_verification_enabled"])
        self.assertIsNone(controller.snapshot()["speaker_model"])
        self.frames(controller, 800, 400, 0.1)
        self.final(controller, 800, 1200, "继续聊")
        self.frames(controller, 1200, 500)
        await self.drained(controller)
        self.assertEqual(sum(e["type"] == "speech_end" for e in events), 2)
        self.assertEqual(controller.epoch, 2)

    async def test_disabled_identity_does_not_load_camplus_or_registry(self):
        source, playback, _ = self.make()
        source.speakers.enabled = False
        with (
            patch("oopz_mcp.listening.CamPlus") as encoder,
            patch("oopz_mcp.listening.SpeakerRegistry") as registry,
            patch("oopz_mcp.listening.SmartTurn", return_value=Detector()) as detector,
        ):
            controller = ListeningController.prepare(source.profile, playback, lambda _: None)
        self.controllers.append(controller)
        encoder.assert_not_called()
        registry.assert_not_called()
        detector.assert_called_once()

    async def test_unknown_speech_neither_pauses_output_nor_publishes_input(self):
        controller, playback, events = self.make()
        playback.begin()
        self.frames(controller, 0, 1000, 0.3)
        self.final(controller, 0, 1000, "陌生人的话")
        self.frames(controller, 1000, 500)
        await self.drained(controller)
        self.assertFalse(playback.speech_pending)
        self.assertTrue(playback.active)
        self.assertEqual(events, [])
        self.assertEqual(controller.snapshot()["filtered_segments"], 1)

    async def test_late_asr_from_previous_registered_person_is_not_assigned_to_next_person(self):
        controller, _, events = self.make()
        self.frames(controller, 0, 1000, 0.1)
        self.frames(controller, 1000, 1000, 0.2)
        self.final(controller, 1000, 2000, "乙先到的转写")
        self.frames(controller, 2000, 500)
        await self.drained(controller)
        self.final(controller, 0, 1000, "甲迟到的转写")
        await self.drained(controller)
        transcripts = {
            (item["speaker_id"], item["text"]) for item in events if item["type"] == "transcript"
        }
        self.assertEqual(transcripts, {("A", "甲迟到的转写"), ("B", "乙先到的转写")})
        terminal = {
            item["speaker_id"]
            for item in events
            if item["type"] in {"speech_end", "input_uncertain", "speech_superseded"}
        }
        self.assertEqual(terminal, {"A", "B"}, "an old speaker must not remain pending forever")

    async def test_reply_readiness_requires_both_asr_final_and_semantic_completion(self):
        detector = Detector()
        controller, playback, events = self.make(detector)
        self.frames(controller, 0, 1000, 0.1)
        self.frames(controller, 1000, 500)
        await self.drained(controller)
        self.assertTrue(playback.speech_pending)
        self.assertEqual(detector.calls, 0)
        self.assertFalse(any(item["type"] == "speech_end" for item in events))
        self.final(controller, 0, 1000, "完整的句子")
        await self.drained(controller)
        self.assertEqual(detector.calls, 1)
        self.assertEqual(
            [item["type"] for item in events], ["speaker_activity", "transcript", "speech_end"]
        )

    async def test_short_unverified_sound_is_ignored_and_recognized_backchannel_is_held(self):
        controller, playback, events = self.make()
        self.frames(controller, 0, 200, 0.1)
        self.final(controller, 0, 200, "嗯")
        self.frames(controller, 200, 500)
        await self.drained(controller)
        self.assertFalse(playback.speech_pending)
        self.assertEqual(events, [])
        self.frames(controller, 700, 600, 0.1)
        self.final(controller, 700, 1300, "嗯")
        self.frames(controller, 1300, 500)
        await self.drained(controller)
        transcripts = [item for item in events if item["type"] == "transcript"]
        self.assertEqual(len(transcripts), 1)
        self.assertTrue(transcripts[0]["final"])
        ends = [item for item in events if item["type"] == "speech_end"]
        self.assertEqual(len(ends), 1)
        self.assertFalse(ends[0]["notify"])

    async def test_semantic_continue_is_not_misreported_as_missing_asr_final(self):
        controller, _, events = self.make(Detector(0.1))
        self.frames(controller, 0, 1000, 0.1)
        self.final(controller, 0, 1000, "我还想问一下")
        self.frames(controller, 1000, 3000)
        await self.drained(controller)
        self.assertFalse(any(item["type"] in {"fault", "speech_end"} for item in events))
        self.frames(controller, 4000, 5000)
        await self.drained(controller)
        uncertain = [item for item in events if item["type"] == "input_uncertain"]
        self.assertEqual(len(uncertain), 1)
        self.assertEqual(uncertain[0]["reason"], "semantic_end_uncertain")
        self.assertFalse(any(item["type"] == "fault" for item in events))

    async def test_new_speech_during_end_inference_invalidates_old_completion(self):
        detector = BlockingDetector()
        self.addCleanup(detector.release.set)
        controller, _, events = self.make(detector)
        self.frames(controller, 0, 1000, 0.1)
        self.final(controller, 0, 1000, "我想问")
        self.frames(controller, 1000, 500)
        await self.until(detector.entered.is_set)
        self.frames(controller, 1500, 600, 0.1)
        self.frames(controller, 2100, 500)
        detector.release.set()
        await self.drained(controller)
        self.assertFalse(any(item["type"] == "speech_end" for item in events))
        self.final(controller, 1500, 2100, "还有这一件事")
        await self.drained(controller)
        self.assertEqual(sum(item["type"] == "speech_end" for item in events), 1)
        self.assertGreaterEqual(detector.calls, 2)

    async def test_timed_out_model_faults_once_and_late_result_cannot_emit_after_close(self):
        detector = BlockingDetector()
        self.addCleanup(detector.release.set)
        controller, _, events = self.make(detector)
        self.frames(controller, 0, 1000, 0.1)
        self.final(controller, 0, 1000, "模型超时测试")
        self.frames(controller, 1000, 500)
        await self.until(lambda: any(item["type"] == "fault" for item in events))
        self.assertEqual(sum(item["type"] == "fault" for item in events), 1)
        await controller.close()
        previous = list(events)
        detector.release.set()
        self.frames(controller, 1500, 1000, 0.2)
        self.final(controller, 1500, 2500, "关闭后的结果")
        await asyncio.sleep(0.03)
        self.assertEqual(events, previous)

    async def test_mixed_registered_and_unknown_audio_cannot_publish_a_transcript(self):
        for second_speaker in (0.2, 0.3):
            controller, _, events = self.make()
            self.frames(controller, 0, 1000, 0.1)
            self.frames(controller, 1000, 1000, second_speaker)
            self.final(controller, 0, 2000, "不能确定文字来自哪个人")
            self.frames(controller, 2000, 500)
            await self.drained(controller)
            self.assertFalse(any(item["type"] == "transcript" for item in events))
            self.assertFalse(any(item["type"] == "speech_end" for item in events))
            self.assertEqual(controller.snapshot()["filtered_segments"], 1)

    async def test_genuinely_missing_asr_final_still_faults(self):
        controller, _, events = self.make()
        self.frames(controller, 0, 1000, 0.1)
        self.frames(controller, 1000, 2200)
        await self.drained(controller)
        faults = [item["code"] for item in events if item["type"] == "fault"]
        self.assertEqual(faults, ["xfyun_final_timeout"])

    async def test_continuous_registered_utterance_keeps_a_short_final_audio_tail(self):
        for duration in (1200, 1400, 2200):
            with self.subTest(duration_ms=duration):
                controller, _, events = self.make()
                self.frames(controller, 0, duration, 0.1)
                self.final(controller, 0, duration, "连续说话的末尾不能丢")
                self.frames(controller, duration, 500)
                await self.drained(controller)
                transcripts = [event for event in events if event["type"] == "transcript"]
                self.assertEqual(
                    [(event["speaker_id"], event["text"]) for event in transcripts],
                    [("A", "连续说话的末尾不能丢")],
                )
                self.assertEqual(sum(event["type"] == "speech_end" for event in events), 1)
                self.assertFalse(
                    any(event["type"] in {"input_uncertain", "fault"} for event in events)
                )

    async def test_short_unknown_after_silence_cannot_inherit_the_registered_speaker(self):
        controller, playback, events = self.make(Detector(0.1))
        self.frames(controller, 0, 1000, 0.1)
        self.final(controller, 0, 1000, "注册者尚未说完")
        self.frames(controller, 1000, 500)
        await self.drained(controller)
        last_verified_voice = playback.voice_at
        self.frames(controller, 1500, 200, 0.3)
        self.final(controller, 1500, 1700, "陌生人的短句")
        self.frames(controller, 1700, 500)
        await self.drained(controller)
        self.assertEqual(playback.voice_at, last_verified_voice)
        self.assertEqual(
            [event["text"] for event in events if event["type"] == "transcript"], ["注册者尚未说完"]
        )
        self.assertEqual(controller.snapshot()["filtered_segments"], 1)
        self.assertEqual(sum(event["type"] == "speaker_activity" for event in events), 1)
