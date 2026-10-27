import unittest

import numpy as np
import test_listening as fixtures


class RecordingEncoder:
    def __init__(self):
        self.calls = []

    def embedding(self, pcm):
        self.calls.append(pcm.copy())
        voice = pcm[np.abs(pcm) >= 0.01]
        return float(voice.mean()) if voice.size else 0.0


class DominantSpeakerEncoder(RecordingEncoder):
    def embedding(self, pcm):
        self.calls.append(pcm.copy())
        registered = np.count_nonzero(np.isclose(pcm, 0.1))
        unknown = np.count_nonzero(np.isclose(pcm, 0.3))
        return 0.1 if registered > unknown else 0.3


class ListeningAccumulationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.helper = fixtures.ListeningTests()
        await self.helper.asyncSetUp()

    async def asyncTearDown(self):
        await self.helper.asyncTearDown()

    def make(self, encoder=None):
        controller, playback, events = self.helper.make()
        controller.speakers.window_ms = 2000
        controller.encoder = encoder or RecordingEncoder()
        return controller, playback, events

    def quiet(self, controller, start, duration):
        for at in range(start, start + duration, 20):
            controller.feed_frame(
                np.full(320, 0.001, dtype=np.float32),
                False,
                100.0 + (at + 20) / 1000,
                at,
                at + 20,
            )

    async def known_prefix(self, encoder=None):
        controller, playback, events = self.make(encoder)
        self.helper.frames(controller, 0, 2000, 0.1)
        await self.helper.drained(controller)
        self.assertEqual([event["type"] for event in events], ["speaker_activity"])
        return controller, playback, events

    async def test_sparse_speech_accumulates_past_window_without_early_pause(self):
        controller, playback, events = self.make()
        playback.begin()
        for start in range(0, 2000, 500):
            self.helper.frames(controller, start, 100, 0.1)
            self.quiet(controller, start + 100, 400)
        await self.helper.drained(controller)

        self.assertEqual(events, [])
        self.assertEqual(controller.encoder.calls, [])
        self.assertFalse(playback.speech_pending)
        self.assertTrue(playback.active)

        self.helper.frames(controller, 2000, 200, 0.1)
        await self.helper.drained(controller)
        self.assertEqual([event["type"] for event in events], ["speaker_activity"])
        self.assertTrue(playback.speech_pending)
        self.helper.final(controller, 0, 2200, "间隔自然但语音证据已经足够")
        await self.helper.drained(controller)
        self.assertEqual(sum(event["type"] == "transcript" for event in events), 1)

    async def test_continuous_tail_with_small_quiet_gaps_is_verified_on_its_own(self):
        for leading_gap in (0, 20, 100):
            with self.subTest(leading_gap=leading_gap):
                controller, playback, events = self.make()
                self.helper.frames(controller, 0, 1920, 0.1)
                self.quiet(controller, 1920, 80)
                await self.helper.drained(controller)
                self.assertEqual(len(controller.encoder.calls), 1)

                self.quiet(controller, 2000, leading_gap)
                start = 2000 + leading_gap
                self.helper.frames(controller, start, 60, 0.1)
                self.quiet(controller, start + 60, 20)
                self.helper.frames(controller, start + 80, 60, 0.1)
                self.quiet(controller, start + 140, 100)
                self.helper.frames(controller, start + 240, 80, 0.1)
                self.quiet(controller, start + 320, 500)
                await self.helper.drained(controller)

                self.assertEqual(len(controller.encoder.calls), 2)
                self.assertEqual(controller.encoder.calls[-1].size, 320 * 16)
                self.assertAlmostEqual(playback.voice_at, 100 + (start + 320) / 1000)
                self.assertEqual(sum(event["type"] == "speaker_activity" for event in events), 1)
                self.assertFalse(any(event["type"] == "fault" for event in events))

    async def test_unknown_short_tail_cannot_be_masked_by_registered_history(self):
        for gap, duration in ((0, 200), (20, 200), (100, 400)):
            with self.subTest(gap=gap, duration=duration):
                encoder = DominantSpeakerEncoder()
                controller, playback, _ = await self.known_prefix(encoder)
                previous_voice_at = playback.voice_at
                self.quiet(controller, 2000, gap)
                self.helper.frames(controller, 2000 + gap, duration, 0.3)
                self.quiet(controller, 2000 + gap + duration, 500)
                await self.helper.drained(controller)

                self.assertEqual(len(encoder.calls), 2)
                np.testing.assert_array_equal(
                    encoder.calls[-1], np.full(duration * 16, 0.3, dtype=np.float32)
                )
                self.assertEqual(playback.voice_at, previous_voice_at)
                assert controller.active is not None
                self.assertEqual(controller.active.last_voice_ms, 2000)

    async def test_insufficient_candidate_is_bounded_and_its_final_waits_for_classification(self):
        controller, playback, events = self.make()
        for start in range(0, 2000, 500):
            self.helper.frames(controller, start, 60, 0.1)
            self.quiet(controller, start + 60, 440)
        self.helper.final(controller, 0, 2000, "尚无足够身份依据")
        await self.helper.drained(controller)

        self.assertEqual(events, [])
        self.assertEqual(controller.filtered_segments, 0)
        self.assertEqual(len(controller.finals), 1)
        for start in (2000, 2500):
            self.helper.frames(controller, start, 60, 0.1)
            self.quiet(controller, start + 60, 440)
        await self.helper.drained(controller)

        self.assertEqual(controller.processed_ms, 3000)
        self.assertEqual(controller.block, [])
        self.assertEqual(len(controller.finals), 0)
        self.assertEqual(controller.filtered_segments, 1)
        self.assertEqual(controller.encoder.calls, [])
        self.assertFalse(playback.speech_pending)
        self.assertEqual(events, [])

    async def test_continuous_tail_below_two_hundred_ms_remains_unverified(self):
        controller, playback, _ = await self.known_prefix()
        previous_voice_at = playback.voice_at
        self.helper.frames(controller, 2000, 180, 0.1)
        self.quiet(controller, 2180, 500)
        await self.helper.drained(controller)

        self.assertEqual(len(controller.encoder.calls), 1)
        self.assertEqual(playback.voice_at, previous_voice_at)
        self.assertTrue(
            all(frame.epoch == 0 for frame in controller.history if frame.start >= 2000)
        )

    async def test_new_short_segment_cannot_borrow_identity_across_endpoint_silence(self):
        controller, playback, events = await self.known_prefix()
        previous_voice_at = playback.voice_at
        self.quiet(controller, 2000, 500)
        self.helper.frames(controller, 2500, 200, 0.1)
        self.quiet(controller, 2700, 500)
        await self.helper.drained(controller)

        self.assertEqual(len(controller.encoder.calls), 1)
        self.assertEqual(playback.voice_at, previous_voice_at)
        self.assertTrue(
            all(frame.epoch == 0 for frame in controller.history if frame.start >= 2500)
        )
        self.assertEqual(sum(event["type"] == "speaker_activity" for event in events), 1)

    async def test_two_second_windows_reject_a_final_spanning_different_speakers(self):
        for second_speaker in (0.2, 0.3):
            with self.subTest(second_speaker=second_speaker):
                controller, _, events = self.make()
                self.helper.frames(controller, 0, 2000, 0.1)
                self.helper.frames(controller, 2000, 2000, second_speaker)
                self.helper.final(controller, 0, 4000, "来自不同人的混合片段")
                self.quiet(controller, 4000, 500)
                await self.helper.drained(controller)

                self.assertFalse(any(event["type"] == "transcript" for event in events))
                self.assertEqual(controller.filtered_segments, 1)
                self.assertTrue(any(event["type"] == "input_uncertain" for event in events))

    async def test_registered_short_segment_cannot_resume_across_rejected_unknown_speech(self):
        controller, playback, events = await self.known_prefix(DominantSpeakerEncoder())
        previous_voice_at = playback.voice_at
        self.helper.frames(controller, 2000, 200, 0.3)
        self.quiet(controller, 2200, 500)
        await self.helper.drained(controller)
        self.assertEqual(len(controller.encoder.calls), 2)

        self.helper.frames(controller, 2700, 200, 0.1)
        self.quiet(controller, 2900, 500)
        await self.helper.drained(controller)

        self.assertEqual(len(controller.encoder.calls), 2)
        self.assertEqual(playback.voice_at, previous_voice_at)
        self.assertTrue(
            all(frame.epoch == 0 for frame in controller.history if frame.start >= 2000)
        )
        self.assertEqual(sum(event["type"] == "speaker_activity" for event in events), 1)

    async def test_ambiguous_mixture_inside_one_two_second_window_is_not_published(self):
        # The mean-based fake rejects both mixtures; this does not assert model accuracy.
        for second_speaker in (0.2, 0.4):
            with self.subTest(second_speaker=second_speaker):
                controller, playback, events = self.make()
                playback.begin()
                self.helper.frames(controller, 0, 1000, 0.1)
                self.helper.frames(controller, 1000, 1000, second_speaker)
                self.helper.final(controller, 0, 2000, "同一窗口内无法归属的混合片段")
                self.quiet(controller, 2000, 500)
                await self.helper.drained(controller)

                self.assertEqual(controller.filtered_segments, 1)
                self.assertFalse(playback.speech_pending)
                self.assertTrue(playback.active)
                self.assertEqual(events, [])
