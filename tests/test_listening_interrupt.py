"""Repeated controller/session interruption checks with synthetic audio and mock TTS."""

import unittest

import test_listening as fixtures

from oopz_mcp.backends import MockBackend
from oopz_mcp.config import Profile, Reply, Settings
from oopz_mcp.session import Session


class ListeningInterruptTests(unittest.IsolatedAsyncioTestCase):
    SHAPES = {
        "short_sentence": ((600, True),),
        "natural_gap": ((800, True), (200, False), (700, True)),
        "short_tail_after_window": ((2000, True), (200, False), (400, True)),
    }

    async def asyncSetUp(self):
        # Composition reuses frame/model fixtures without rediscovering their test methods.
        self.fixture = fixtures.ListeningTests()
        await self.fixture.asyncSetUp()
        self.sessions = []

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()
        for session in self.sessions:
            await session.close()

    async def make(self):
        controller, _, _ = self.fixture.make()
        controller.speakers.window_ms = 2000
        settings = Settings()
        backend = MockBackend(Profile(), settings.limits)
        session = Session("integration-fixture", controller.profile, settings, backend)
        self.sessions.append(session)
        controller.playback = backend.playback
        controller.emit = session.receive
        await session.start()
        return controller, session, backend

    def utterance(self, controller, start, shape, speaker):
        cursor = start
        for duration, voiced in shape:
            self.fixture.frames(controller, cursor, duration, speaker if voiced else 0.0)
            cursor += duration
        self.fixture.frames(controller, cursor, 500)
        return cursor, cursor + 500

    def reply(self, session, reply_id):
        return Reply(
            reply_id=reply_id,
            reply_to_turn_id=session.last_turn_id,
            based_on_input_revision=session.input_revision,
            text="这是持续播放中的模拟回复，用于确认插话先暂停，稳定文字再取消旧输出，结束后能继续。"
            * 3,
        )

    async def speaking(self, session, backend, reply_id, expected_syntheses):
        self.assertEqual(session.accept_reply(self.reply(session, reply_id)), "accepted")
        await self.fixture.until(
            lambda: (
                backend.synthesis_count == expected_syntheses and session.playback.local_samples > 0
            )
        )
        self.assertTrue(session.playback.active)
        self.assertFalse(session.playback.speech_pending)

    async def test_registered_interruptions_resume_for_three_shapes_three_times_each(self):
        rounds = pauses = cancellations = completions = replies = 0
        for name, shape in self.SHAPES.items():
            controller, session, backend = await self.make()
            await self.speaking(session, backend, f"{name}-opening", 1)
            cursor = 0
            for iteration in range(3):
                with self.subTest(shape=name, repetition=iteration + 1):
                    detector = fixtures.BlockingDetector()
                    self.addCleanup(detector.release.set)
                    controller.detector = detector
                    old_epoch = session.playback.epoch
                    old_revision = session.input_revision
                    start = cursor
                    end, cursor = self.utterance(controller, start, shape, 0.1)
                    await self.fixture.until(
                        lambda controller=controller, cursor=cursor, session=session: (
                            controller.processed_ms >= cursor and session.playback.speech_pending
                        )
                    )
                    self.assertTrue(session.playback.active)
                    self.assertEqual(session.playback.epoch, old_epoch)
                    self.assertEqual(session.input_revision, old_revision)
                    self.assertEqual(detector.calls, 0, "ASR final is still outstanding")
                    pauses += 1

                    self.fixture.final(controller, start, end, f"第{iteration + 1}次插话")
                    await self.fixture.until(detector.entered.is_set)
                    self.assertEqual(session.input_revision, old_revision + 1)
                    self.assertGreater(session.playback.epoch, old_epoch)
                    self.assertFalse(session.playback.active)
                    self.assertTrue(session.playback.speech_pending)
                    self.assertEqual(session.playback.stop_reason, "new_user_input")
                    self.assertEqual(backend.synthesis_count, iteration + 1)
                    cancellations += 1

                    detector.release.set()
                    await self.fixture.until(
                        lambda session=session: not session.playback.speech_pending
                    )
                    ends = [e for e in session.events if e["type"] == "input_complete"]
                    self.assertEqual(len(ends), iteration + 1)
                    self.assertEqual(ends[-1]["completion_reason"], "smart_turn")
                    self.assertEqual(ends[-1]["speaker_id"], "A")
                    self.assertEqual(ends[-1]["input_revision"], session.input_revision)
                    self.assertEqual(detector.calls, 1)
                    self.assertFalse(session.input_blocked)
                    completions += 1

                    await self.speaking(
                        session, backend, f"{name}-answer-{iteration}", iteration + 2
                    )
                    replies += 1
                    rounds += 1

            turns = [e for e in session.events if e["type"] == "user_turn"]
            self.assertEqual(len(turns), 3)
            self.assertEqual({e["speaker_id"] for e in turns}, {"A"})
            self.assertEqual(session.input_revision, 3)
            self.assertEqual(backend.synthesis_count, 4)
            self.assertEqual(controller.filtered_segments, 0)
            self.assertEqual(controller.uncertain_turns, 0)
            self.assertEqual(session.status, "ready")
            await controller.close()
            await session.close()
        self.assertEqual((rounds, pauses, cancellations, completions, replies), (9, 9, 9, 9, 9))

    async def test_unknown_speech_preserves_active_output_for_three_shapes_three_times_each(self):
        rounds = filtered = uninterrupted = 0
        for name, shape in self.SHAPES.items():
            controller, session, backend = await self.make()
            await self.speaking(session, backend, f"{name}-opening", 1)
            output_epoch = session.playback.epoch
            cursor = 0
            for iteration in range(3):
                with self.subTest(shape=name, repetition=iteration + 1):
                    start = cursor
                    end, cursor = self.utterance(controller, start, shape, 0.3)
                    self.fixture.final(controller, start, end, "未注册者在播报期间讲话")
                    await self.fixture.until(
                        lambda controller=controller, cursor=cursor, iteration=iteration: (
                            controller.processed_ms >= cursor
                            and controller.filtered_segments == iteration + 1
                        )
                    )
                    self.assertTrue(session.playback.active)
                    self.assertFalse(session.playback.speech_pending)
                    self.assertEqual(session.playback.voice_at, 0.0)
                    self.assertEqual(session.playback.epoch, output_epoch)
                    self.assertEqual(session.playback.stop_reason, "")
                    self.assertEqual(session.input_revision, 0)
                    self.assertIsNone(session.last_turn_id)
                    self.assertFalse(session.input_blocked)
                    self.assertEqual(list(session.events), [])
                    self.assertEqual(backend.synthesis_count, 1)
                    self.assertEqual(controller.detector.calls, 0)
                    self.assertEqual(session.status, "ready")
                    rounds += 1
                    filtered += 1
                    uninterrupted += 1
            self.assertEqual(controller.filtered_segments, 3)
            await controller.close()
            await session.close()
        self.assertEqual((rounds, filtered, uninterrupted), (9, 9, 9))


if __name__ == "__main__":
    unittest.main()
