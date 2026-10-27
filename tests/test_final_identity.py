import threading
import unittest

import numpy as np
import test_listening as fixtures

from oopz_mcp.listening import AudioSlice, SpeechTurn


class RecordingEncoder:
    def __init__(self, results=None):
        self.calls = []
        self.results = iter(results) if results is not None else None

    def embedding(self, pcm):
        self.calls.append(pcm.copy())
        return next(self.results) if self.results is not None else float(pcm.mean())


class BlockingEncoder(RecordingEncoder):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def embedding(self, pcm):
        self.entered.set()
        try:
            self.release.wait(3)
            return super().embedding(pcm)
        finally:
            self.finished.set()


class FinalIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Composition reuses fixture helpers without discovering their tests twice.
        self.helper = fixtures.ListeningTests()
        await self.helper.asyncSetUp()

    async def asyncTearDown(self):
        await self.helper.asyncTearDown()

    def prepared(self, pcm, encoder):
        controller, _, events = self.helper.make()
        controller.encoder = encoder
        duration = pcm.size // 16
        for start in range(0, duration, 20):
            controller.history.append(
                AudioSlice(
                    start,
                    start + 20,
                    100.0 + (start + 20) / 1000,
                    pcm[start * 16 : (start + 20) * 16],
                    True,
                    "A",
                    1,
                )
            )
        turn = SpeechTurn(1, "A", duration, 100.0 + duration / 1000)
        controller.turns.append(turn)
        controller.active = turn
        controller.epoch = 1
        controller.processed_ms = duration
        return controller, events

    async def test_block_identity_cannot_authorize_unknown_or_short_final_subsegments(self):
        for value, duration, accepted in ((0.3, 600, False), (0.1, 599, False), (0.1, 600, True)):
            with self.subTest(value=value, duration=duration):
                start, end = 207, 207 + duration
                pcm = np.full(1000 * 16, 0.1, dtype=np.float32)
                pcm[start * 16 : end * 16] = value
                encoder = RecordingEncoder()
                controller, events = self.prepared(pcm, encoder)
                self.helper.final(controller, start, end, "只验证这个子区间")
                await self.helper.drained(controller)

                transcripts = [event for event in events if event["type"] == "transcript"]
                self.assertEqual(bool(transcripts), accepted)
                if duration < 600:
                    self.assertEqual(encoder.calls, [])
                else:
                    self.assertEqual(len(encoder.calls), 1)
                    np.testing.assert_array_equal(encoder.calls[0], pcm[start * 16 : end * 16])
                if not accepted:
                    self.assertEqual(events[-1]["reason"], "final_speaker_unverified")

    async def test_long_final_verifies_every_bounded_chunk_and_clips_both_edges(self):
        for last_result, accepted in ((0.1, True), (0.3, False)):
            with self.subTest(last_result=last_result):
                pcm = np.linspace(0.09, 0.11, 30400 * 16, dtype=np.float32)
                encoder = RecordingEncoder([0.1, 0.1, last_result])
                controller, events = self.prepared(pcm, encoder)
                self.helper.final(controller, 7, 30357, "超过三十秒的完整区间")
                await self.helper.drained(controller)

                self.assertEqual(len(encoder.calls), 3)
                expected = ((7, 15007), (15007, 30007), (29757, 30357))
                for chunk, (start, end) in zip(encoder.calls, expected, strict=True):
                    self.assertLessEqual(chunk.size, 15 * 16000)
                    np.testing.assert_array_equal(chunk, pcm[start * 16 : end * 16])
                self.assertEqual(any(event["type"] == "transcript" for event in events), accepted)
                if not accepted:
                    self.assertEqual(events[-1]["reason"], "final_speaker_unverified")

    async def test_punctuation_final_closes_superseded_turn_without_another_inference(self):
        encoder = RecordingEncoder()
        controller, events = self.prepared(np.full(16000, 0.1, dtype=np.float32), encoder)
        assert controller.active is not None
        controller.active.superseded = True
        self.helper.final(controller, 0, 1000, " ，。！？... ")
        await self.helper.drained(controller)

        self.assertEqual(encoder.calls, [])
        self.assertTrue(controller.active.closed)
        self.assertEqual([event["type"] for event in events], ["speech_superseded"])

    async def test_close_during_final_inference_discards_the_late_result(self):
        encoder = BlockingEncoder()
        self.addCleanup(encoder.release.set)
        controller, events = self.prepared(np.full(16000, 0.1, dtype=np.float32), encoder)
        self.helper.final(controller, 0, 1000, "关闭之后不应出现")
        await self.helper.until(encoder.entered.is_set)

        await controller.close()
        encoder.release.set()
        await self.helper.until(encoder.finished.is_set)

        self.assertEqual(events, [])
        self.assertTrue(controller.closed)
        assert controller.task is not None
        self.assertTrue(controller.task.done())
