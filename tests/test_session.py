"""Offline behavioral tests. Written for later execution; never open real audio devices."""

import asyncio
import time
import unittest

from oopz_mcp.backends import MockBackend
from oopz_mcp.config import Exchange, Limits, Profile, Reply, Settings
from oopz_mcp.playback import Playback
from oopz_mcp.session import Session, SessionManager, VoiceError


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings()
        self.backend = MockBackend(Profile(), self.settings.limits)
        self.session = Session("mock", Profile(), self.settings, self.backend)
        await self.session.start()
        self.request_number = 0

    async def asyncTearDown(self):
        await self.session.close()

    def request(self, **kwargs):
        self.request_number += 1
        values = dict(
            session_id=self.session.id,
            request_id=f"req-{self.request_number}",
            ack_event_seq=self.session.acked,
            wait_ms=250,
        )
        values.update(kwargs)
        return Exchange.model_validate(values)

    def reply(self, text="完整回复", **kwargs):
        values = dict(
            reply_id="answer-1",
            based_on_input_revision=self.session.input_revision,
            reply_to_turn_id=self.session.last_turn_id,
            text=text,
        )
        values.update(kwargs)
        return Reply.model_validate(values)

    async def test_retry_does_not_replay_and_conflicting_id_is_rejected(self):
        request = self.request(reply=self.reply())
        first = await self.session.exchange(request)
        second = await self.session.exchange(request)
        self.assertEqual(first, second)
        self.assertEqual(self.backend.synthesis_count, 1)
        with self.assertRaisesRegex(VoiceError, "request_id_conflict"):
            await self.session.exchange(request.model_copy(update={"wait_ms": 500}))

    async def test_reply_id_deduplicates_across_requests(self):
        reply = self.reply()
        await self.session.exchange(self.request(reply=reply))
        result = await self.session.exchange(self.request(reply=reply))
        self.assertEqual(result["reply_status"], "duplicate_no_replay")
        self.assertEqual(self.backend.synthesis_count, 1)
        with self.assertRaisesRegex(VoiceError, "reply_id_conflict"):
            await self.session.exchange(
                self.request(reply=reply.model_copy(update={"text": "别的话"}))
            )

    async def test_unacknowledged_input_survives_retries(self):
        self.backend.inject("第一句话")
        first = await self.session.exchange(self.request())
        second = await self.session.exchange(self.request())
        self.assertEqual(first["events"], second["events"])
        timeout = await self.session.exchange(self.request(ack_event_seq=first["next_event_seq"]))
        self.assertEqual(timeout["reason"], "timeout")
        self.assertEqual(timeout["events"], [])
        self.assertEqual(self.backend.synthesis_count, 0)

    async def test_ack_cannot_consume_undelivered_input(self):
        self.backend.inject("不能丢")
        with self.assertRaisesRegex(VoiceError, "invalid_ack"):
            await self.session.exchange(self.request(ack_event_seq=1))
        result = await self.session.exchange(self.request())
        self.assertEqual(result["events"][0]["text"], "不能丢")

    async def test_stale_reply_rejected_with_new_input(self):
        old = self.reply()
        self.backend.inject("用户改变了问题")
        result = await self.session.exchange(self.request(reply=old))
        self.assertEqual(result["reply_status"], "rejected_stale")
        self.assertEqual(result["input_revision"], 1)
        self.assertEqual(self.backend.synthesis_count, 0)
        self.assertEqual(result["events"][0]["text"], "用户改变了问题")

    async def test_pending_speech_defers_then_invalidates_output(self):
        self.session.playback.voice_evidence(time.monotonic())
        result = await self.session.exchange(self.request(reply=self.reply()))
        self.assertEqual(result["reply_status"], "deferred_while_user_speaks")
        self.assertEqual(self.backend.synthesis_count, 0)
        self.backend.inject("别说刚才那个了")
        await asyncio.sleep(0)
        self.assertFalse(self.session.playback.active)
        self.assertEqual(self.backend.synthesis_count, 0)

    async def test_backchannel_resumes_without_new_revision(self):
        self.session.playback.voice_evidence(time.monotonic())
        await self.session.exchange(self.request(reply=self.reply()))
        self.backend.inject("嗯嗯")
        await asyncio.sleep(0.05)
        self.assertEqual(self.session.input_revision, 0)
        self.assertFalse(self.session.playback.speech_pending)
        self.assertEqual(self.backend.synthesis_count, 1)

    async def test_one_waiter_and_independent_stop_close(self):
        request = self.request(wait_ms=5000)
        pending = asyncio.create_task(self.session.exchange(request))
        await asyncio.sleep(0)
        with self.assertRaisesRegex(VoiceError, "request_in_progress"):
            await self.session.exchange(request)
        with self.assertRaisesRegex(VoiceError, "exchange_busy"):
            await self.session.exchange(self.request())
        self.assertTrue(self.session.stop_output("local_test")["stopped"])
        await self.session.close()
        result = await asyncio.wait_for(pending, 1)
        self.assertEqual(result["reason"], "closed")

    async def test_cancel_closes_gate_and_keeps_future_input(self):
        request = self.request(reply=self.reply(), wait_ms=5000)
        pending = asyncio.create_task(self.session.exchange(request))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(self.session.playback.active)
        retried = await self.session.exchange(request)
        self.assertEqual(retried["reason"], "cancelled")
        self.backend.inject("取消后仍需要交付")
        result = await self.session.exchange(self.request())
        self.assertEqual(result["events"][0]["text"], "取消后仍需要交付")

    async def test_overflow_is_an_acknowledgeable_explicit_gap(self):
        self.session.limits.event_capacity = 2
        for text in ("一", "二", "三", "四"):
            self.backend.inject(text)
        result = await self.session.exchange(self.request())
        gap = result["events"][0]
        self.assertEqual((gap["type"], gap["from_seq"], gap["through_seq"]), ("event_gap", 1, 2))
        self.assertEqual([event["seq"] for event in result["events"]], [2, 3, 4])
        await self.session.exchange(self.request(ack_event_seq=4))
        self.assertEqual(self.session.pending(), [])

    async def test_long_transcript_is_explicitly_chunked_not_truncated(self):
        self.session.limits.transcript_chars = 64
        text = "很长的用户发言" * 30
        self.backend.inject(text, final=False)
        result = await self.session.exchange(self.request())
        self.assertEqual("".join(event["text"] for event in result["events"]), text)
        self.assertTrue(all(not event["segment_final"] for event in result["events"]))
        self.assertTrue(result["speech_pending"])

    async def test_input_complete_wakes_waiter_without_fabricating_new_text(self):
        self.backend.inject("稳定的前半句", final=False)
        first = await self.session.exchange(self.request())
        waiter = asyncio.create_task(
            self.session.exchange(self.request(ack_event_seq=first["next_event_seq"], wait_ms=5000))
        )
        await asyncio.sleep(0)
        self.session.receive({"type": "speech_end", "through": time.monotonic(), "notify": True})
        completed = await asyncio.wait_for(waiter, 1)
        self.assertEqual(completed["events"][0]["type"], "input_complete")
        self.assertEqual(completed["input_revision"], first["input_revision"])
        self.assertNotIn("text", completed["events"][0])
        self.assertFalse(completed["speech_pending"])

    async def test_id_capacity_fails_closed_instead_of_forgetting_ids(self):
        self.session.limits.request_capacity = 4
        self.backend.inject("保持未确认")
        requests = [self.request() for _ in range(4)]
        for request in requests:
            await self.session.exchange(request)
        with self.assertRaisesRegex(VoiceError, "request_capacity"):
            await self.session.exchange(self.request(reply=self.reply()))
        self.assertEqual((await self.session.exchange(requests[0]))["reason"], "user_turn")
        self.assertEqual(self.backend.synthesis_count, 0)

    async def test_backend_failure_interrupts_waiter(self):
        pending = asyncio.create_task(self.session.exchange(self.request(wait_ms=5000)))
        await asyncio.sleep(0)
        self.session.receive({"type": "fault", "code": "audio_device_stalled"})
        result = await asyncio.wait_for(pending, 1)
        self.assertEqual(result["session_status"], "fault")
        self.assertEqual(result["events"][0]["code"], "audio_device_stalled")
        self.assertFalse(self.session.playback.active)

    async def test_real_wait_timeout_does_not_synthesize_filler(self):
        before = time.monotonic()
        result = await self.session.exchange(self.request())
        self.assertEqual(result["reason"], "timeout")
        self.assertGreaterEqual(time.monotonic() - before, 0.24)
        self.assertEqual(self.backend.synthesis_count, 0)

    def speaker_input(self, speaker_id, epoch, text, *, final=False):
        self.session.receive(
            {
                "type": "transcript",
                "speaker_id": speaker_id,
                "speech_epoch": epoch,
                "text": text,
                "final": final,
                "through": float(epoch * 10),
                "last_voice_at": float(epoch * 10),
                "endpoint_at": float(epoch * 10 + 1),
            }
        )

    async def test_historical_speaker_input_cannot_revise_or_interrupt_current_reply(self):
        self.session.receive({"type": "speaker_activity", "speaker_id": "B", "speech_epoch": 2})
        self.speaker_input("B", 2, "现在回答乙的问题", final=True)
        self.assertEqual(self.session.accept_reply(self.reply()), "accepted")
        revision = self.session.input_revision
        output_epoch = self.session.playback.epoch
        output_task = self.session.output_task
        self.speaker_input("A", 1, "甲迟到的结果", final=True)
        self.assertEqual(self.session.input_revision, revision)
        self.assertEqual(self.session.playback.epoch, output_epoch)
        self.assertIs(self.session.output_task, output_task)
        self.assertTrue(self.session.playback.active)
        history = [event for event in self.session.pending() if event["type"] == "historical_input"]
        self.assertEqual(
            [(event["speaker_id"], event["text"]) for event in history], [("A", "甲迟到的结果")]
        )
        self.assertNotIn("input_revision", history[0])

    async def test_previous_speaker_end_cannot_release_new_speaker_pause(self):
        self.session.receive({"type": "speaker_activity", "speaker_id": "B", "speech_epoch": 2})
        self.session.playback.voice_evidence(20.0)
        self.session.receive(
            {
                "type": "speech_end",
                "speaker_id": "A",
                "speech_epoch": 1,
                "through": 999.0,
                "notify": True,
            }
        )
        self.assertTrue(self.session.playback.speech_pending)
        self.assertEqual(self.session.pending(), [])
        self.session.receive(
            {
                "type": "speech_end",
                "speaker_id": "B",
                "speech_epoch": 2,
                "through": 20.0,
                "notify": False,
            }
        )
        self.assertFalse(self.session.playback.speech_pending)

    async def test_uncertain_turn_blocks_reply_until_new_verified_input(self):
        self.session.receive({"type": "speaker_activity", "speaker_id": "A", "speech_epoch": 1})
        self.speaker_input("A", 1, "还没确认说完")
        self.session.receive(
            {
                "type": "input_uncertain",
                "speaker_id": "A",
                "speech_epoch": 1,
                "reason": "semantic_end_uncertain",
            }
        )
        self.assertEqual(self.session.accept_reply(self.reply()), "rejected_input_uncertain")
        self.session.receive({"type": "speaker_activity", "speaker_id": "B", "speech_epoch": 2})
        self.assertEqual(self.session.accept_reply(self.reply()), "rejected_input_uncertain")
        self.speaker_input("A", 1, "旧话轮迟到的一段")
        self.assertEqual(self.session.accept_reply(self.reply()), "rejected_input_uncertain")
        self.speaker_input("B", 2, "现在是一个完整的新问题", final=True)
        self.assertEqual(self.session.accept_reply(self.reply()), "accepted")


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_discards_queued_and_late_pcm(self):
        gate = Playback(16000, 20, 40)
        epoch = gate.begin()
        await gate.enqueue(epoch, b"\x01\x00" * 320)
        gate.stop("explicit")
        self.assertFalse(await gate.enqueue(epoch, b"\x02\x00" * 320))
        output = bytearray(640)
        gate.render_into(output)
        self.assertEqual(output, bytes(640))
        new_epoch = gate.begin()
        await gate.enqueue(new_epoch, b"\x03\x00" * 320)
        gate.render_into(output)
        self.assertEqual(output, b"\x03\x00" * 320)

    async def test_pause_preserves_pcm_until_resume(self):
        gate = Playback(16000, 20, 40)
        epoch = gate.begin()
        await gate.enqueue(epoch, b"\x01\x00" * 320)
        at = time.monotonic()
        gate.voice_evidence(at)
        output = bytearray(640)
        gate.render_into(output)
        self.assertEqual(output, bytes(640))
        self.assertEqual(len(gate.queue), 1)
        gate.release_speech(at)
        gate.render_into(output)
        self.assertEqual(output, b"\x01\x00" * 320)

    async def test_old_endpoint_cannot_resume_new_speech(self):
        gate = Playback(16000, 20, 40)
        gate.voice_evidence(20.0)
        gate.voice_evidence(9.0)
        gate.release_speech(10.0)
        self.assertTrue(gate.speech_pending)

    async def test_bounded_queue_unblocks_producer_on_stop(self):
        gate = Playback(16000, 20, 20)
        epoch = gate.begin()
        await gate.enqueue(epoch, bytes(640))
        producer = asyncio.create_task(gate.enqueue(epoch, bytes(640)))
        await asyncio.sleep(0.01)
        self.assertFalse(producer.done())
        gate.stop("stop")
        self.assertFalse(await asyncio.wait_for(producer, 1))


class ManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_expiry_stops_and_removes_session(self):
        manager = SessionManager(Settings(limits=Limits()))
        opened = await manager.open("mock")
        session = manager.get(opened["session_id"])
        session.expires_at = 0
        await manager.expire()
        self.assertFalse(session.playback.active)
        self.assertEqual(session.status, "closed")
        self.assertEqual(manager.sessions, {})

    async def test_disconnect_cleanup_closes_all_output(self):
        manager = SessionManager(Settings())
        opened = await manager.open("mock")
        session = manager.get(opened["session_id"])
        await manager.close_all()
        self.assertFalse(session.playback.active)
        self.assertIsInstance(session.backend, MockBackend)
        if isinstance(session.backend, MockBackend):
            self.assertIsNotNone(session.backend.task)
            assert session.backend.task is not None
            self.assertTrue(session.backend.task.done())
        self.assertEqual(manager.sessions, {})


if __name__ == "__main__":
    unittest.main()
