import asyncio
import unittest

import httpx

from oopz_mcp.backends import MockBackend
from oopz_mcp.config import Exchange, InstantConfig, Profile, Reply, Settings
from oopz_mcp.instant import InstantClient, InstantError, InstantResult
from oopz_mcp.session import Session


class ControlledInstant(InstantClient):
    def __init__(self, *, error=None):
        super().__init__(
            InstantConfig(model="offline-fixture"),
            httpx.MockTransport(lambda _: httpx.Response(500)),
        )
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()
        self.cancelled = 0
        self.error = error
        self.result = InstantResult.model_validate(
            {
                "speech": "请主代理确认这个请求。",
                "task": {
                    "kind": "action",
                    "summary": "请求打开台灯。",
                    "action": "light.on",
                    "target": "台灯",
                    "parameters": [],
                    "needs_confirmation": True,
                },
            }
        )

    async def generate(self, text, *, on_speech=None, on_progress=None):
        self.calls.append(text)
        self.started.set()
        try:
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    # Deliberately simulate an inference backend that returns after cancellation.
                    self.cancelled += 1
            if self.error:
                raise InstantError(self.error)
            return self.result
        finally:
            self.finished.set()


class TwoPhaseInstant(ControlledInstant):
    def __init__(self):
        super().__init__()
        self.releases = [asyncio.Event(), asyncio.Event()]
        self.finishes = [asyncio.Event(), asyncio.Event()]

    async def generate(self, text, *, on_speech=None, on_progress=None):
        index = len(self.calls)
        self.calls.append(text)
        try:
            while not self.releases[index].is_set():
                try:
                    await self.releases[index].wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
            return self.result
        finally:
            self.finishes[index].set()


class StreamingInstant(ControlledInstant):
    def __init__(self, *, error=None, repeated_speech=False):
        super().__init__(error=error)
        self.speech_release = asyncio.Event()
        self.speech_sent = asyncio.Event()
        self.repeated_speech = repeated_speech

    async def wait_for_release(self, event):
        while not event.is_set():
            try:
                await event.wait()
            except asyncio.CancelledError:
                self.cancelled += 1

    async def generate(self, text, *, on_speech=None, on_progress=None):
        self.calls.append(text)
        self.started.set()
        try:
            await self.wait_for_release(self.speech_release)
            if on_progress:
                on_progress("first_content", 0.01)
                on_progress("speech_ready", 0.02)
            if on_speech:
                await on_speech(self.result.speech)
                if self.repeated_speech:
                    await on_speech(self.result.speech)
            self.speech_sent.set()
            await self.wait_for_release(self.release)
            if self.error:
                raise InstantError(self.error)
            if on_progress:
                on_progress("json_complete", 0.05)
            return self.result
        finally:
            self.finished.set()


class InstantSessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resources = []

    async def asyncTearDown(self):
        for session, _, instant in self.resources:
            instant.release.set()
            if isinstance(instant, StreamingInstant):
                instant.speech_release.set()
            await asyncio.wait_for(session.close(), 2)

    async def open(self, *, instant=None, **options):
        settings = Settings()
        backend = MockBackend(Profile(), settings.limits)
        instant = instant or ControlledInstant(**options)
        session = Session("mock", Profile(), settings, backend, instant=instant)
        self.resources.append((session, backend, instant))
        await session.start()
        return session, backend, instant

    async def until(self, predicate):
        async with asyncio.timeout(1):
            while not predicate():  # noqa: ASYNC110 - bounded offline task observation
                await asyncio.sleep(0.001)

    def activity(self, session, speaker="A", epoch=1):
        session.receive({"type": "speaker_activity", "speaker_id": speaker, "speech_epoch": epoch})

    def segment(self, session, text, *, speaker="A", epoch=1, final=False):
        at = float(epoch * 10)
        session.playback.voice_evidence(at)
        session.receive(
            {
                "type": "transcript",
                "speaker_id": speaker,
                "speech_epoch": epoch,
                "text": text,
                "final": final,
                "through": at,
                "last_voice_at": at,
                "endpoint_at": at + 1,
            }
        )

    def complete(self, session, *, speaker="A", epoch=1, **extra):
        session.receive(
            {
                "type": "speech_end",
                "speaker_id": speaker,
                "speech_epoch": epoch,
                "through": float(epoch * 10),
                "notify": True,
                "completion_reason": "smart_turn",
                **extra,
            }
        )

    def events(self, session, kind):
        return [event for event in session.pending() if event["type"] == kind]

    def reply(self, session, reply_id="agent-answer"):
        return Reply(
            reply_id=reply_id,
            reply_to_turn_id=session.last_turn_id,
            based_on_input_revision=session.input_revision,
            text="主代理的完整回复。",
        )

    async def start_pending(self, **options):
        session, backend, instant = await self.open(**options)
        self.activity(session)
        self.segment(session, "请打开台灯。")
        self.complete(session)
        await self.until(instant.started.is_set)
        return session, backend, instant

    async def test_segments_survive_ack_and_duplicate_ends_generate_once(self):
        session, backend, instant = await self.open()
        self.activity(session)
        self.segment(session, "请帮我")
        delivered = session.result("user_turn", "none")
        session.acknowledge(delivered["next_event_seq"])
        self.segment(session, "打开台灯。", final=True)
        await asyncio.sleep(0)
        self.assertEqual(instant.calls, [])
        self.complete(session)
        self.complete(session)
        await self.until(instant.started.is_set)
        self.assertEqual(instant.calls, ["请帮我\n打开台灯。"])
        self.assertTrue(self.events(session, "input_complete"))
        instant.release.set()
        await self.until(lambda: bool(self.events(session, "agent_task")))
        await self.until(lambda: backend.synthesis_count == 1)
        task = self.events(session, "agent_task")[0]
        self.assertEqual(task["task"], instant.result.task.model_dump())
        self.assertEqual(task["speech"], instant.result.speech)
        self.assertEqual(task["input_revision"], session.input_revision)
        self.assertEqual(task["turn_id"], session.last_turn_id)
        self.assertEqual((task["speaker_id"], task["speech_epoch"]), ("A", 1))
        self.assertEqual(task["source"], "local_instant_model")
        self.assertFalse(task["trusted"])
        self.assertEqual(task["execution_status"], "not_started")
        self.assertTrue(task["task_id"])
        self.assertEqual(task["quick_reply"]["status"], "scheduled")
        self.assertIn(task["quick_reply"]["reply_id"], session.replies)
        self.assertEqual(task["quick_reply"]["output_epoch"], session.playback.epoch)
        self.complete(session)
        await asyncio.sleep(0)
        self.assertEqual(len(instant.calls), 1)
        self.assertEqual(len(self.events(session, "agent_task")), 1)

    async def test_input_limit_keeps_original_transcript_and_never_sends_truncated_text(self):
        session, backend, instant = await self.open()
        self.activity(session)
        pieces = ("甲" * 5000, "乙" * 3000)
        for text in pieces:
            self.segment(session, text)
        self.complete(session)
        await self.until(lambda: bool(self.events(session, "instant_error")))
        error = self.events(session, "instant_error")[0]
        self.assertEqual(error["code"], "instant_input_too_large")
        self.assertEqual(error["fallback"], "agent_uses_original_transcript")
        self.assertEqual(error["input_revision"], session.input_revision)
        self.assertEqual(error["turn_id"], session.last_turn_id)
        self.assertEqual(instant.calls, [])
        self.assertEqual(backend.synthesis_count, 0)
        self.assertEqual(
            "".join(event["text"] for event in self.events(session, "user_turn")), "".join(pieces)
        )

    async def test_exact_eight_thousand_characters_are_not_rejected(self):
        session, _, instant = await self.open()
        self.activity(session)
        self.segment(session, "甲" * 8000)
        self.complete(session)
        await self.until(
            lambda: instant.started.is_set() or bool(self.events(session, "instant_error"))
        )
        self.assertEqual(instant.calls, ["甲" * 8000])

    async def test_sixty_fifth_segment_falls_back_without_inference(self):
        session, _, instant = await self.open()
        self.activity(session)
        for number in range(65):
            self.segment(session, f"第{number}段")
        self.complete(session)
        self.assertEqual(instant.calls, [])
        error = self.events(session, "instant_error")[0]
        self.assertEqual(error["code"], "instant_input_too_large")
        self.assertEqual(error["fallback"], "agent_uses_original_transcript")

    async def test_raw_mock_and_unfinished_verified_input_do_not_trigger(self):
        session, backend, instant = await self.open()
        backend.inject("未经声纹验证的原始mock输入", final=True)
        self.complete(session)
        await asyncio.sleep(0)
        self.assertEqual(instant.calls, [])
        self.activity(session)
        self.segment(session, "已验身份但尚未判停", final=True)
        self.complete(session, completion_reason="asr_final")
        self.complete(session, notify=False)
        await asyncio.sleep(0)
        self.assertEqual(instant.calls, [])
        self.complete(session)
        await self.until(instant.started.is_set)
        self.assertEqual(instant.calls, ["已验身份但尚未判停"])

    async def test_agent_reply_wins_even_when_cancelled_inference_returns(self):
        session, backend, instant = await self.start_pending()
        self.assertEqual(session.accept_reply(self.reply(session)), "accepted")
        output_epoch = session.playback.epoch
        await self.until(lambda: instant.cancelled > 0)
        instant.release.set()
        await self.until(instant.finished.is_set)
        await self.until(lambda: backend.synthesis_count == 1)
        self.assertEqual(self.events(session, "agent_task"), [])
        self.assertEqual(session.playback.epoch, output_epoch)

    async def test_agent_reply_can_replace_completed_local_quick_reply(self):
        session, backend, instant = await self.start_pending()
        instant.release.set()
        await self.until(lambda: backend.synthesis_count == 1)
        local_epoch = session.playback.epoch
        self.assertEqual(session.accept_reply(self.reply(session)), "accepted")
        await self.until(lambda: backend.synthesis_count == 2)
        self.assertGreater(session.playback.epoch, local_epoch)
        self.assertEqual(len(self.events(session, "agent_task")), 1)

    async def test_invalidated_results_never_publish_or_play(self):
        for reason in ("stop", "new_epoch", "new_segment", "uncertain", "fault", "close"):
            with self.subTest(reason=reason):
                session, backend, instant = await self.start_pending()
                closing = None
                if reason == "stop":
                    session.stop_output("explicit_stop")
                elif reason == "new_epoch":
                    self.activity(session, "B", 2)
                elif reason == "new_segment":
                    self.segment(session, "还有一件事")
                elif reason == "uncertain":
                    session.receive(
                        {
                            "type": "input_uncertain",
                            "speaker_id": "A",
                            "speech_epoch": 1,
                            "reason": "semantic_end_uncertain",
                        }
                    )
                elif reason == "fault":
                    session.receive({"type": "fault", "code": "audio_device_stalled"})
                else:
                    closing = asyncio.create_task(session.close())
                await self.until(lambda instant=instant: instant.cancelled > 0)
                instant.release.set()
                if closing:
                    await asyncio.wait_for(closing, 1)
                await self.until(instant.finished.is_set)
                self.assertEqual(self.events(session, "agent_task"), [])
                self.assertEqual(backend.synthesis_count, 0)
                self.assertFalse(session.playback.active)

    async def test_failure_is_linked_to_original_turn_and_keeps_transcript_for_fallback(self):
        session, backend, instant = await self.start_pending(error="instant_timeout")
        instant.release.set()
        await self.until(lambda: bool(self.events(session, "instant_error")))
        error = self.events(session, "instant_error")[0]
        self.assertEqual(error["code"], "instant_timeout")
        self.assertEqual(error["fallback"], "agent_uses_original_transcript")
        self.assertEqual(error["input_revision"], session.input_revision)
        self.assertEqual(error["turn_id"], session.last_turn_id)
        self.assertTrue(error["task_id"])
        self.assertTrue(self.events(session, "user_turn"))
        self.assertEqual(backend.synthesis_count, 0)

    async def test_streamed_speech_starts_before_task_and_is_not_replayed_at_json_complete(self):
        instant = StreamingInstant(repeated_speech=True)
        session, backend, _ = await self.start_pending(instant=instant)
        instant.speech_release.set()
        await self.until(lambda: backend.synthesis_count == 1)
        output_epoch = session.playback.epoch
        self.assertTrue(session.state()["instant"]["pending"])
        self.assertEqual(self.events(session, "agent_task"), [])
        durations = session.metrics.snapshot()["durations"]
        self.assertAlmostEqual(durations["instant_request_to_first_content"]["p50_ms"], 10)
        self.assertAlmostEqual(durations["instant_request_to_speech_ready"]["p50_ms"], 20)
        self.assertNotIn("instant_request_to_json_complete", durations)
        # Completing playback before the JSON arrives must not make its output epoch stale.
        await self.until(lambda: not session.playback.active)
        instant.release.set()
        await self.until(lambda: bool(self.events(session, "agent_task")))
        task = self.events(session, "agent_task")[0]
        self.assertEqual(task["quick_reply"]["output_epoch"], output_epoch)
        self.assertEqual(task["quick_reply"]["status"], "scheduled")
        self.assertEqual(backend.synthesis_count, 1)
        self.assertEqual(session.playback.epoch, output_epoch)
        self.assertFalse(session.state()["instant"]["pending"])
        durations = session.metrics.snapshot()["durations"]
        self.assertAlmostEqual(durations["instant_request_to_json_complete"]["p50_ms"], 50)

    async def test_stream_tail_failure_stops_only_early_output_and_exposes_quick_reply(self):
        for code in ("instant_invalid_response", "instant_timeout"):
            with self.subTest(code=code):
                instant = StreamingInstant(error=code)
                session, backend, _ = await self.start_pending(instant=instant)
                instant.speech_release.set()
                await self.until(lambda backend=backend: backend.synthesis_count == 1)
                early_epoch = session.playback.epoch
                instant.release.set()
                await self.until(
                    lambda session=session: bool(self.events(session, "instant_error"))
                )
                error = self.events(session, "instant_error")[-1]
                self.assertEqual(error["code"], code)
                self.assertEqual(error["fallback"], "agent_uses_original_transcript")
                self.assertEqual(error["quick_reply"]["status"], "scheduled")
                self.assertEqual(error["quick_reply"]["output_epoch"], early_epoch)
                self.assertIn(error["quick_reply"]["reply_id"], session.replies)
                self.assertEqual(self.events(session, "agent_task"), [])
                self.assertFalse(session.playback.active)
                self.assertGreater(session.playback.epoch, early_epoch)
                self.assertEqual(backend.synthesis_count, 1)
                self.assertFalse(session.state()["instant"]["pending"])

    async def test_agent_reply_after_early_speech_survives_late_success_or_failure(self):
        for error in (None, "instant_invalid_response"):
            with self.subTest(error=error):
                instant = StreamingInstant(error=error)
                session, backend, _ = await self.start_pending(instant=instant)
                instant.speech_release.set()
                await self.until(lambda backend=backend: backend.synthesis_count == 1)
                self.assertEqual(session.accept_reply(self.reply(session)), "accepted")
                agent_epoch = session.playback.epoch
                await self.until(lambda instant=instant: instant.cancelled > 0)
                instant.release.set()
                await self.until(instant.finished.is_set)
                await self.until(lambda backend=backend: backend.synthesis_count == 2)
                self.assertEqual(session.playback.epoch, agent_epoch)
                self.assertEqual(self.events(session, "agent_task"), [])
                errors = self.events(session, "instant_error")
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["fallback"], "none")
                self.assertIn("quick_reply", errors[0])
                self.assertNotIn(
                    "instant_request_to_json_complete", session.metrics.snapshot()["durations"]
                )

    async def test_cancelled_stream_cannot_start_early_speech_or_resume_it(self):
        for early in (False, True):
            for reason in ("stop", "new_epoch", "new_segment", "uncertain", "fault", "close"):
                with self.subTest(early=early, reason=reason):
                    instant = StreamingInstant()
                    session, backend, _ = await self.start_pending(instant=instant)
                    if early:
                        instant.speech_release.set()
                        await self.until(lambda backend=backend: backend.synthesis_count == 1)
                    closing = None
                    if reason == "stop":
                        session.stop_output("explicit_stop")
                    elif reason == "new_epoch":
                        self.activity(session, "B", 2)
                    elif reason == "new_segment":
                        self.segment(session, "还有一件事")
                    elif reason == "uncertain":
                        session.receive(
                            {
                                "type": "input_uncertain",
                                "speaker_id": "A",
                                "speech_epoch": 1,
                                "reason": "semantic_end_uncertain",
                            }
                        )
                    elif reason == "fault":
                        session.receive({"type": "fault", "code": "audio_device_stalled"})
                    else:
                        closing = asyncio.create_task(session.close())
                    await self.until(lambda instant=instant: instant.cancelled > 0)
                    instant.speech_release.set()
                    instant.release.set()
                    if closing:
                        await asyncio.wait_for(closing, 1)
                    await self.until(instant.finished.is_set)
                    self.assertEqual(self.events(session, "agent_task"), [])
                    self.assertEqual(backend.synthesis_count, int(early))
                    self.assertFalse(session.playback.active)
                    errors = self.events(session, "instant_error")
                    self.assertEqual(len(errors), 1)
                    self.assertEqual("quick_reply" in errors[0], early)

    async def test_final_speech_mismatch_stops_early_output_without_task(self):
        instant = StreamingInstant()
        session, backend, _ = await self.start_pending(instant=instant)
        instant.speech_release.set()
        await self.until(lambda: backend.synthesis_count == 1)
        instant.result = instant.result.model_copy(update={"speech": "另一段不一致的确认。"})
        instant.release.set()
        await self.until(lambda: bool(self.events(session, "instant_error")))
        self.assertEqual(
            self.events(session, "instant_error")[-1]["code"], "instant_speech_changed"
        )
        self.assertEqual(self.events(session, "agent_task"), [])
        self.assertEqual(backend.synthesis_count, 1)
        self.assertFalse(session.playback.active)

    async def test_early_tts_failure_returns_original_input_without_publishing_late_task(self):
        instant = StreamingInstant()
        session, backend, _ = await self.start_pending(instant=instant)

        async def failed_synthesis(text):
            backend.synthesis_count += 1
            yield b"\0" * backend.playback.frame_bytes
            raise RuntimeError("offline TTS failure")

        backend.synthesize = failed_synthesis
        instant.speech_release.set()
        await self.until(lambda: bool(self.events(session, "output_error")))
        error = self.events(session, "instant_error")[-1]
        self.assertEqual(error["reason"], "tts_failed")
        self.assertEqual(error["fallback"], "agent_uses_original_transcript")
        self.assertEqual(error["quick_reply"]["status"], "scheduled")
        instant.release.set()
        await self.until(instant.finished.is_set)
        self.assertEqual(self.events(session, "agent_task"), [])
        self.assertEqual(backend.synthesis_count, 1)
        self.assertFalse(session.playback.active)

    async def test_changed_output_epoch_rejects_task_even_without_new_input(self):
        instant = StreamingInstant()
        session, backend, _ = await self.start_pending(instant=instant)
        instant.speech_release.set()
        await self.until(lambda: backend.synthesis_count == 1)
        output_epoch = session.playback.stop("independent_gate_stop")
        instant.release.set()
        await self.until(instant.finished.is_set)
        await self.until(lambda: bool(self.events(session, "instant_error")))
        self.assertEqual(self.events(session, "instant_error")[-1]["reason"], "output_superseded")
        self.assertEqual(self.events(session, "agent_task"), [])
        self.assertEqual(session.playback.epoch, output_epoch)
        self.assertEqual(backend.synthesis_count, 1)
        self.assertFalse(session.playback.active)

    async def test_reply_capacity_remains_bounded(self):
        session, backend, instant = await self.open()
        session.limits.request_capacity = 4
        self.activity(session)
        self.segment(session, "保持这个输入版本", final=True)
        for number in range(4):
            self.assertEqual(
                session.accept_reply(self.reply(session, f"agent-{number}")), "accepted"
            )
        self.assertEqual(
            session.accept_reply(self.reply(session, "too-many")), "rejected_reply_capacity"
        )
        self.activity(session, "B", 2)
        self.segment(session, "新的话轮也不能无限增加ID", speaker="B", epoch=2)
        self.complete(session, speaker="B", epoch=2)
        self.assertEqual(len(session.replies), 4)
        self.assertEqual(instant.calls, [])
        self.assertEqual(self.events(session, "instant_error")[-1]["code"], "instant_capacity")
        self.assertEqual(backend.synthesis_count, 0)

    async def test_historical_a_transcript_does_not_pollute_current_b_input(self):
        session, _, instant = await self.open()
        self.activity(session)
        self.segment(session, "甲之前的问题")
        self.activity(session, "B", 2)
        self.segment(session, "乙现在的问题", speaker="B", epoch=2)
        self.segment(session, "甲迟到的转写", speaker="A", epoch=1)
        self.complete(session, speaker="B", epoch=2)
        await self.until(instant.started.is_set)
        self.assertEqual(instant.calls, ["乙现在的问题"])
        history = self.events(session, "historical_input")
        self.assertEqual(
            [(item["speaker_id"], item["text"]) for item in history], [("A", "甲迟到的转写")]
        )
        instant.release.set()
        await self.until(lambda: bool(self.events(session, "agent_task")))
        self.assertEqual(self.events(session, "agent_task")[0]["speaker_id"], "B")

    async def test_cancelled_a_finishing_cannot_clear_b_pending_task(self):
        instant = TwoPhaseInstant()
        session, _, _ = await self.open(instant=instant)
        try:
            self.activity(session)
            self.segment(session, "甲的问题")
            self.complete(session)
            await self.until(lambda: len(instant.calls) == 1)
            self.activity(session, "B", 2)
            self.segment(session, "乙的问题", speaker="B", epoch=2)
            self.complete(session, speaker="B", epoch=2)
            await self.until(lambda: len(instant.calls) == 2)
            b_task = session.instant_task
            self.assertIsNotNone(b_task)
            instant.releases[0].set()
            await self.until(instant.finishes[0].is_set)
            self.assertIs(session.instant_task, b_task)
            assert b_task is not None
            self.assertFalse(b_task.done())
            self.assertEqual(self.events(session, "agent_task"), [])

            instant.releases[1].set()
            await self.until(lambda: bool(self.events(session, "agent_task")))
            tasks = self.events(session, "agent_task")
            self.assertEqual(
                [(task["speaker_id"], task["speech_epoch"]) for task in tasks], [("B", 2)]
            )
            self.assertEqual(instant.calls, ["甲的问题", "乙的问题"])
        finally:
            for release in instant.releases:
                release.set()

    async def test_agent_task_replay_and_ack_keep_ids_without_regeneration_or_replay(self):
        session, backend, instant = await self.start_pending()
        instant.release.set()
        await self.until(lambda: backend.synthesis_count == 1)
        request = Exchange(
            session_id=session.id, request_id="deliver-task", ack_event_seq=0, wait_ms=250
        )
        first = await session.exchange(request)
        replay = await session.exchange(request)
        self.assertEqual(first, replay)
        task = next(event for event in first["events"] if event["type"] == "agent_task")
        acknowledged = await session.exchange(
            Exchange(
                session_id=session.id,
                request_id="ack-task",
                ack_event_seq=first["next_event_seq"],
                wait_ms=250,
            )
        )
        self.assertEqual(acknowledged["events"], [])
        old_retry = await session.exchange(request)
        retried_task = next(event for event in old_retry["events"] if event["type"] == "agent_task")
        self.assertEqual(retried_task["task_id"], task["task_id"])
        self.assertEqual(retried_task["quick_reply"]["reply_id"], task["quick_reply"]["reply_id"])
        self.assertEqual(len(instant.calls), 1)
        self.assertEqual(backend.synthesis_count, 1)
