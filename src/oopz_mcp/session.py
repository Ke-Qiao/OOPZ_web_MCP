from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import math
import time
import uuid
from collections import deque
from dataclasses import dataclass

import anyio

from .backends import LiveBackend, MockBackend, make_backend
from .config import Exchange, Profile, Reply, Settings
from .instant import MAX_INPUT_CHARS, InstantClient, InstantError
from .metrics import Metrics


class VoiceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class RequestRecord:
    fingerprint: str
    result: dict | None = None


class Session:
    def __init__(
        self,
        profile_name: str,
        profile: Profile,
        settings: Settings,
        backend: MockBackend | LiveBackend | None = None,
        *,
        instant: InstantClient | None = None,
    ):
        self.id = "s-" + uuid.uuid4().hex
        self.profile_name = profile_name
        self.profile = profile
        self.limits = settings.limits
        self.backend = backend or make_backend(profile, self.limits)
        self.playback = self.backend.playback
        self.metrics = Metrics(settings.log_metrics)
        self.events: deque[dict] = deque()
        self.seq = self.acked = self.delivered = self.dropped_through = 0
        self.input_revision = 0
        self.last_turn_id: str | None = None
        self.active_speech_epoch = 0
        self.last_input_speech_epoch = 0
        self.active_speaker_id: str | None = None
        self.input_blocked = False
        self.last_delivered_input: tuple[int, float] | None = None
        self.changed = asyncio.Event()
        self.requests: dict[str, RequestRecord] = {}
        self.replies: dict[str, str] = {}
        self.waiter: str | None = None
        self.output_task: asyncio.Task | None = None
        self.output_tasks: set[asyncio.Task] = set()
        self.cleanup_task: asyncio.Task | None = None
        self.output_context: dict | None = None
        self.status = "opening"
        self.readiness: dict = {}
        self.expires_at = time.monotonic() + self.limits.lease_seconds
        self.instant = instant or (InstantClient(profile.instant) if profile.instant else None)
        self.instant_task: asyncio.Task | None = None
        self.instant_tasks: set[asyncio.Task] = set()
        self.instant_context: dict | None = None
        self.instant_generation = 0
        self.instant_attempted_revision = -1
        self.agent_reply_revision = -1
        self.instant_text = ""
        self.instant_segments = 0
        self.instant_overflow = False
        self.instant_speech_epoch = 0
        self.instant_speaker_id: str | None = None

    async def start(self) -> None:
        self.readiness = await self.backend.start(self.receive)
        self.status = "ready"
        self.touch()
        self.metrics.mark("session_ready", backend=self.profile.backend)

    def touch(self) -> None:
        self.expires_at = time.monotonic() + self.limits.lease_seconds

    def append(self, event_type: str, **data) -> None:
        self.seq += 1
        while len(self.events) >= self.limits.event_capacity:
            self.dropped_through = self.events.popleft()["seq"]
        self.events.append({"seq": self.seq, "type": event_type, **data})
        self.changed.set()

    def _cancel_instant(self, reason: str, *, stop_quick_reply: bool = True) -> None:
        self.instant_generation += 1
        context = self.instant_context
        if self.instant_context is not None:
            self.append(
                "instant_error",
                **self.instant_context,
                code="instant_cancelled",
                reason=reason,
                fallback="agent_uses_original_transcript" if reason == "tts_failed" else "none",
            )
            self.instant_context = None
        if self.instant_task and self.instant_task is not asyncio.current_task():
            self.instant_task.cancel()
        self.instant_task = None
        if stop_quick_reply and context is not None:
            quick_reply = context.get("quick_reply")
            if quick_reply and quick_reply["output_epoch"] == self.playback.epoch:
                self.stop_output(reason, cancel_instant=False)

    def _remember_instant(self, event: dict, text: str) -> None:
        if self.instant is None:
            return
        epoch, speaker = event.get("speech_epoch", 0), event.get("speaker_id")
        if (
            epoch <= 0
            or not speaker
            or (epoch, speaker)
            != (
                self.active_speech_epoch,
                self.active_speaker_id,
            )
        ):
            return
        if (epoch, speaker) != (self.instant_speech_epoch, self.instant_speaker_id):
            self.instant_speech_epoch, self.instant_speaker_id = epoch, speaker
            self.instant_text, self.instant_segments, self.instant_overflow = "", 0, False
        self.instant_segments = min(65, self.instant_segments + 1)
        if self.instant_overflow:
            return
        separator = "\n" if self.instant_text else ""
        if (
            self.instant_segments > 64
            or len(self.instant_text) + len(text) + len(separator) > MAX_INPUT_CHARS
        ):
            self.instant_text = ""
            self.instant_overflow = True
            return
        self.instant_text += separator + text

    def _start_instant(self, event: dict) -> None:
        if (
            self.instant is None
            or self.status != "ready"
            or self.input_blocked
            or self.playback.speech_pending
            or event.get("completion_reason") != "smart_turn"
            or event.get("speech_epoch", 0) <= 0
            or event.get("speaker_id") != self.instant_speaker_id
            or event.get("speech_epoch") != self.instant_speech_epoch
            or self.input_revision <= self.instant_attempted_revision
            or self.agent_reply_revision == self.input_revision
        ):
            return
        self.instant_attempted_revision = self.input_revision
        context = {
            "task_id": f"{self.id}:{self.last_turn_id}",
            "turn_id": self.last_turn_id,
            "input_revision": self.input_revision,
            "speaker_id": self.instant_speaker_id,
            "speech_epoch": self.instant_speech_epoch,
        }
        if self.instant_overflow or not self.instant_text:
            self.append(
                "instant_error",
                **context,
                code="instant_input_too_large" if self.instant_overflow else "instant_empty_input",
                fallback="agent_uses_original_transcript",
            )
            return
        if len(self.instant_tasks) >= 2 or len(self.replies) >= self.limits.request_capacity:
            self.append(
                "instant_error",
                **context,
                code="instant_capacity",
                fallback="agent_uses_original_transcript",
            )
            return
        self.instant_context = context
        self.instant_task = asyncio.create_task(
            self._run_instant(self.instant_text, context, self.instant_generation)
        )
        self.instant_tasks.add(self.instant_task)
        self.instant_task.add_done_callback(self.instant_tasks.discard)

    def _instant_current(self, context: dict, generation: int) -> bool:
        return (
            self.status == "ready"
            and not self.input_blocked
            and not self.playback.speech_pending
            and generation == self.instant_generation
            and context["input_revision"] == self.input_revision
            and context["speech_epoch"] == self.active_speech_epoch
            and context["speaker_id"] == self.active_speaker_id
            and self.agent_reply_revision != self.input_revision
        )

    async def _run_instant(self, text: str, context: dict, generation: int) -> None:
        assert self.instant is not None
        started = self.metrics.mark("instant_started", input_revision=context["input_revision"])
        spoken: str | None = None
        progress_seen: set[str] = set()

        def progress(stage: str, elapsed: float) -> None:
            if (
                stage not in {"first_content", "speech_ready", "json_complete"}
                or stage in progress_seen
                or not math.isfinite(elapsed)
                or elapsed < 0
                or not self._instant_current(context, generation)
            ):
                return
            progress_seen.add(stage)
            at = self.metrics.mark(
                "instant_" + stage,
                at=started + elapsed,
                input_revision=context["input_revision"],
            )
            self.metrics.duration("instant_request_to_" + stage, started, at)

        async def speech_ready(speech: str) -> None:
            nonlocal spoken
            if not self._instant_current(context, generation):
                if generation == self.instant_generation:
                    self._cancel_instant("input_no_longer_complete")
                raise asyncio.CancelledError
            quick_reply = context.get("quick_reply")
            if quick_reply:
                if quick_reply["output_epoch"] != self.playback.epoch:
                    self._cancel_instant("output_superseded")
                    raise asyncio.CancelledError
                if speech != spoken:
                    raise InstantError("instant_speech_changed")
                return
            if not speech.strip() or len(speech) > 200:
                raise InstantError("instant_invalid_response")
            reply_id = f"instant-{self.id}-{context['input_revision']}"
            reply = Reply(
                reply_id=reply_id,
                reply_to_turn_id=context["turn_id"],
                based_on_input_revision=context["input_revision"],
                text=speech,
            )
            status = self.accept_reply(reply, origin="instant")
            if status != "accepted":
                raise InstantError("instant_output_rejected")
            spoken = speech
            context["quick_reply"] = {
                "status": "scheduled",
                "reply_id": reply_id,
                "output_epoch": self.playback.epoch,
            }

        try:
            result = await self.instant.generate(text, on_speech=speech_ready, on_progress=progress)
            # The final validated speech must agree with any field already scheduled for TTS.
            await speech_ready(result.speech)
            self.append(
                "agent_task",
                **context,
                speech=result.speech,
                task=result.task.model_dump(),
                source="local_instant_model",
                trusted=False,
                execution_status="not_started",
            )
            finished = self.metrics.mark("instant_ready", input_revision=context["input_revision"])
            self.metrics.duration("instant_request_to_ready", started, finished)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._instant_current(context, generation):
                quick_reply = context.get("quick_reply")
                if quick_reply and quick_reply["output_epoch"] == self.playback.epoch:
                    self.stop_output("instant_tail_failed", cancel_instant=False)
                self.append(
                    "instant_error",
                    **context,
                    code=exc.code if isinstance(exc, InstantError) else "instant_failed",
                    fallback="agent_uses_original_transcript",
                )
            elif generation == self.instant_generation:
                self._cancel_instant("input_no_longer_complete")
        finally:
            if self.instant_task is asyncio.current_task():
                self.instant_task = None
                self.instant_context = None

    def pending(self) -> list[dict]:
        result = []
        if self.acked < self.dropped_through:
            result.append(
                {
                    "seq": self.dropped_through,
                    "type": "event_gap",
                    "from_seq": self.acked + 1,
                    "through_seq": self.dropped_through,
                    "reason": "unacknowledged_events_overflowed",
                }
            )
        result.extend(event for event in self.events if event["seq"] > self.acked)
        return result

    def acknowledge(self, seq: int) -> None:
        if seq < self.acked or seq > self.delivered:
            raise VoiceError("invalid_ack", "ack must be monotonic and cannot exceed delivered seq")
        self.acked = seq
        while self.events and self.events[0]["seq"] <= seq:
            self.events.popleft()

    def receive(self, event: dict) -> None:
        if self.status not in {"opening", "ready"}:
            return
        kind = event["type"]
        if kind == "fault":
            self.stop_output(event["code"])
            self.status = "fault"
            self.append("backend_error", code=event["code"], recoverable=False)
            self.cleanup_task = asyncio.create_task(self.backend.close())
            return
        if kind == "audio_status":
            self.append("audio_status", output_status_count=event["output_status_count"])
            return
        if kind == "speaker_activity":
            if event["speech_epoch"] != self.active_speech_epoch:
                self._cancel_instant("new_verified_speech")
                self.instant_text, self.instant_segments, self.instant_overflow = "", 0, False
            self.active_speech_epoch = event["speech_epoch"]
            self.active_speaker_id = event["speaker_id"]
            return
        if kind == "speech_superseded":
            self.append(
                "input_superseded",
                speaker_id=event["speaker_id"],
                speech_epoch=event["speech_epoch"],
                reason=event["reason"],
            )
            return
        if kind == "input_uncertain":
            if event["speech_epoch"] == self.active_speech_epoch:
                self.input_blocked = True
                self.stop_output("input_uncertain")
            self.append(
                "input_uncertain",
                speaker_id=event["speaker_id"],
                speech_epoch=event["speech_epoch"],
                reason=event["reason"],
            )
            return
        if kind == "speech_end":
            if event.get("speech_epoch", self.active_speech_epoch) != self.active_speech_epoch:
                return
            self.playback.release_speech(event["through"])
            if (
                event["notify"]
                and self.last_turn_id is not None
                and self.last_input_speech_epoch == self.active_speech_epoch
            ):
                self.append(
                    "input_complete",
                    turn_id=self.last_turn_id,
                    input_revision=self.input_revision,
                    **{
                        k: event[k]
                        for k in (
                            "speaker_id",
                            "speech_epoch",
                            "completion_reason",
                            "completion_probability",
                        )
                        if k in event
                    },
                )
                self._start_instant(event)
            self.changed.set()
            return
        text = event["text"].strip()
        if event.get("speech_epoch", self.last_input_speech_epoch) < max(
            self.last_input_speech_epoch, self.active_speech_epoch
        ):
            cap = self.limits.transcript_chars
            pieces = [text[i : i + cap] for i in range(0, len(text), cap)]
            for index, piece in enumerate(pieces):
                self.append(
                    "historical_input",
                    text=piece,
                    part=index + 1,
                    parts=len(pieces),
                    speaker_id=event["speaker_id"],
                    speech_epoch=event["speech_epoch"],
                    trusted=False,
                )
            return
        if event["last_voice_at"]:
            self.metrics.mark("last_voice_sample", at=event["last_voice_at"])
        endpoint_name = event.get("endpoint_metric", "endpoint_complete")
        self.metrics.mark(endpoint_name, at=event["endpoint_at"])
        ready_at = self.metrics.mark("stable_text_ready", chars=len(text))
        if text:
            if event["last_voice_at"]:
                self.metrics.duration(
                    "last_voice_to_" + endpoint_name, event["last_voice_at"], event["endpoint_at"]
                )
            self.metrics.duration(endpoint_name + "_to_text_ready", event["endpoint_at"], ready_at)
            normalized = text.strip("。，！？,.!? ").casefold()
            backchannel = (
                event["final"]
                and self.playback.active
                and normalized in {x.casefold() for x in self.profile.backchannels}
            )
            if backchannel:
                self.metrics.mark("backchannel_resume")
            else:
                self.input_blocked = False
                self.input_revision += 1
                self.last_input_speech_epoch = event.get("speech_epoch", 0)
                self.last_turn_id = f"u{self.input_revision}"
                self.stop_output("new_user_input")
                self._remember_instant(event, text)
                cap = self.limits.transcript_chars
                pieces = [text[i : i + cap] for i in range(0, len(text), cap)]
                for index, piece in enumerate(pieces):
                    self.append(
                        "user_turn",
                        turn_id=self.last_turn_id,
                        text=piece,
                        input_revision=self.input_revision,
                        segment_final=event["final"],
                        part=index + 1,
                        parts=len(pieces),
                        source="external_channel_audio",
                        trusted=False,
                        text_ready_at=ready_at,
                        **{k: event[k] for k in ("speaker_id", "speech_epoch") if k in event},
                    )
        if (
            event["final"]
            and event.get("speech_epoch", self.active_speech_epoch) == self.active_speech_epoch
        ):
            self.playback.release_speech(event["through"])
        self.changed.set()

    def record_playback(self) -> None:
        context = self.output_context
        at = self.playback.first_output_at
        if context and context["epoch"] == self.playback.epoch and at is not None:
            if not context["first_recorded"]:
                context["first_recorded"] = True
                self.metrics.mark(
                    "first_local_output",
                    at=at,
                    output_epoch=context["epoch"],
                    reply_chars=context["chars"],
                    first_reply_in_session=context["first_reply_in_session"],
                )
                self.metrics.duration("reply_to_first_local_output", context["arrived"], at)

    def stop_output(self, reason: str, *, cancel_instant: bool = True) -> dict:
        if cancel_instant:
            self._cancel_instant(reason, stop_quick_reply=False)
        self.record_playback()
        requested = time.monotonic()
        epoch = self.playback.stop(reason)
        closed = self.metrics.mark("output_gate_closed", reason=reason, output_epoch=epoch)
        self.metrics.duration("local_stop_to_gate", requested, closed)
        if self.output_task and self.output_task is not asyncio.current_task():
            self.output_task.cancel()
        return {
            "stopped": True,
            "output_epoch": epoch,
            "scope": "local_gate_only",
            "native_inference_cancelled": "not_guaranteed",
        }

    async def _speak(self, reply: Reply, epoch: int) -> None:
        try:
            async with asyncio.timeout(self.limits.max_output_seconds):
                while self.playback.speech_pending:
                    if epoch != self.playback.epoch or self.status != "ready":
                        return
                    await asyncio.sleep(0.01)
                if (
                    epoch != self.playback.epoch
                    or reply.based_on_input_revision != self.input_revision
                ):
                    return
                first = True
                async with contextlib.aclosing(self.backend.synthesize(reply.text)) as chunks:
                    async for pcm in chunks:
                        if epoch != self.playback.epoch or self.status != "ready":
                            return
                        if first:
                            at = self.metrics.mark("tts_first_chunk", output_epoch=epoch)
                            if self.output_context and self.output_context["epoch"] == epoch:
                                self.metrics.duration(
                                    "reply_to_tts_first_chunk", self.output_context["arrived"], at
                                )
                            first = False
                        if not await self.playback.enqueue(epoch, pcm):
                            return
                self.playback.finish(epoch)
        except asyncio.CancelledError:
            raise
        except Exception:
            if epoch == self.playback.epoch:
                self.stop_output("tts_failed")
                self.append(
                    "output_error",
                    code="tts_failed",
                    reply_id=reply.reply_id,
                    action="inspect_configuration_or_service_before_retry",
                )

    def accept_reply(self, reply: Reply | None, *, origin: str = "agent") -> str:
        if reply is None:
            return "none"
        fingerprint = hashlib.sha256(reply.model_dump_json().encode()).hexdigest()
        previous = self.replies.get(reply.reply_id)
        if previous is not None:
            if previous != fingerprint:
                raise VoiceError(
                    "reply_id_conflict", "reply_id was already used with other content"
                )
            return "duplicate_no_replay"
        if (
            reply.based_on_input_revision != self.input_revision
            or reply.reply_to_turn_id != self.last_turn_id
        ):
            return "rejected_stale"
        if self.status != "ready":
            return "rejected_session_not_ready"
        if self.input_blocked:
            return "rejected_input_uncertain"
        if len(self.replies) >= self.limits.request_capacity:
            return "rejected_reply_capacity"
        if origin == "agent":
            self.agent_reply_revision = self.input_revision
        self.replies[reply.reply_id] = fingerprint
        self.stop_output("replaced_by_new_reply", cancel_instant=origin != "instant")
        epoch = self.playback.begin()
        arrived = self.metrics.mark(
            "reply_parameters_arrived", reply_chars=len(reply.text), output_epoch=epoch
        )
        self.output_context = {
            "epoch": epoch,
            "arrived": arrived,
            "chars": len(reply.text),
            "first_reply_in_session": len(self.replies) == 1,
            "first_recorded": False,
        }
        if self.last_delivered_input and self.last_delivered_input[0] == self.input_revision:
            self.metrics.duration(
                "result_to_reply_parameters", self.last_delivered_input[1], arrived
            )
        self.output_task = asyncio.create_task(self._speak(reply, epoch))
        self.output_tasks.add(self.output_task)
        self.output_task.add_done_callback(self.output_tasks.discard)
        return "deferred_while_user_speaks" if self.playback.speech_pending else "accepted"

    def result(self, reason: str, reply_status: str) -> dict:
        events = self.pending()
        previously_delivered = self.delivered
        if events:
            self.delivered = max(self.delivered, events[-1]["seq"])
        now = self.metrics.mark("exchange_result_ready", events=len(events), reason=reason)
        for event in events:
            if "text_ready_at" in event and event["seq"] > previously_delivered:
                self.metrics.duration("text_ready_to_result", event["text_ready_at"], now)
                self.last_delivered_input = (event["input_revision"], now)
        return {
            "reason": reason,
            "session_id": self.id,
            "input_revision": self.input_revision,
            "next_event_seq": self.delivered,
            "events": copy.deepcopy(events),
            "reply_status": reply_status,
            "speech_pending": self.playback.speech_pending,
            "output_epoch": self.playback.epoch,
            "session_status": self.status,
        }

    async def exchange(self, request: Exchange) -> dict:
        if request.session_id != self.id:
            raise VoiceError("unknown_session", "session_id does not match")
        fingerprint = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        prior = self.requests.get(request.request_id)
        if prior:
            if prior.fingerprint != fingerprint:
                raise VoiceError(
                    "request_id_conflict", "same request_id requires identical parameters"
                )
            if prior.result is None:
                raise VoiceError("request_in_progress", "retry after the original call completes")
            self.touch()
            return copy.deepcopy(prior.result)
        if self.waiter is not None:
            raise VoiceError("exchange_busy", "only one exchange waiter is allowed per session")
        if request.wait_ms > self.limits.max_wait_ms:
            raise VoiceError("wait_too_long", "wait_ms exceeds configured max_wait_ms")
        if len(self.requests) >= self.limits.request_capacity:
            raise VoiceError("request_capacity", "close and reopen session; IDs are never evicted")
        # Validate conflicting reply IDs before applying an acknowledgement.
        if request.reply and request.reply.reply_id in self.replies:
            known = self.replies[request.reply.reply_id]
            proposed = hashlib.sha256(request.reply.model_dump_json().encode()).hexdigest()
            if known != proposed:
                raise VoiceError(
                    "reply_id_conflict", "reply_id was already used with other content"
                )
        self.acknowledge(request.ack_event_seq)
        self.touch()
        record = RequestRecord(fingerprint)
        self.requests[request.request_id] = record
        self.waiter = request.request_id
        reply_status = "none"
        try:
            reply_status = self.accept_reply(request.reply)
            deadline = time.monotonic() + request.wait_ms / 1000
            while True:
                self.changed.clear()
                events = self.pending()
                if events:
                    reason = (
                        "user_turn" if any(x["type"] == "user_turn" for x in events) else "events"
                    )
                    break
                if reply_status.startswith("rejected"):
                    reason = "stale_reply" if reply_status == "rejected_stale" else self.status
                    break
                if self.status != "ready":
                    reason = self.status
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "timeout"
                    break
                try:
                    await asyncio.wait_for(self.changed.wait(), timeout=remaining)
                except TimeoutError:
                    reason = "timeout"
                    break
            record.result = self.result(reason, reply_status)
            return copy.deepcopy(record.result)
        except asyncio.CancelledError:
            self.stop_output("tool_cancelled")
            # No events are marked delivered by a call cancelled before returning.
            record.result = {
                "reason": "cancelled",
                "session_id": self.id,
                "input_revision": self.input_revision,
                "next_event_seq": self.delivered,
                "events": [],
                "reply_status": "cancelled_no_replay",
                "output_epoch": self.playback.epoch,
            }
            raise
        finally:
            self.waiter = None

    def state(self) -> dict:
        self.record_playback()
        return {
            "session_id": self.id,
            "profile": self.profile_name,
            "status": self.status,
            "readiness": self.readiness,
            "oopz_channel_joined": "not_managed",
            "input_revision": self.input_revision,
            "last_turn_id": self.last_turn_id,
            "ack_event_seq": self.acked,
            "last_delivered_seq": self.delivered,
            "latest_event_seq": self.seq,
            "unacknowledged_events": len(self.events),
            "dropped_through_seq": self.dropped_through,
            "request_slots_used": len(self.requests),
            "request_capacity": self.limits.request_capacity,
            "exchange_waiting": self.waiter is not None,
            "lease_remaining_seconds": max(0, self.expires_at - time.monotonic()),
            "playback": self.playback.snapshot(),
            "metrics": self.metrics.snapshot(),
            "input_blocked": self.input_blocked,
            "instant": {
                "enabled": self.instant is not None,
                "pending": self.instant_context is not None,
                "model": self.profile.instant.model if self.profile.instant else None,
                "buffered_chars": len(self.instant_text),
                "input_overflow": self.instant_overflow,
            },
            "listening": (
                self.backend.listening.snapshot()
                if isinstance(self.backend, LiveBackend) and self.backend.listening
                else None
            ),
        }

    async def close(self, reason: str = "closed") -> dict:
        self.stop_output(reason)
        self.status = "closed"
        self.changed.set()
        for task in list(self.instant_tasks):
            task.cancel()
        if self.instant_tasks:
            await asyncio.gather(*self.instant_tasks, return_exceptions=True)
        if self.instant:
            await self.instant.aclose()
        self.instant_text = ""
        for task in list(self.output_tasks):
            task.cancel()
        if self.output_tasks:
            await asyncio.gather(*self.output_tasks, return_exceptions=True)
        if self.cleanup_task:
            await self.cleanup_task
        else:
            self.cleanup_task = asyncio.create_task(self.backend.close())
            await self.cleanup_task
        return {"session_id": self.id, "closed": True, "reason": reason}


class SessionManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.sessions: dict[str, Session] = {}
        self.opening = False
        self.shutting_down = False

    def get(self, session_id: str) -> Session:
        try:
            return self.sessions[session_id]
        except KeyError:
            raise VoiceError(
                "unknown_session", "session is closed, expired, or belongs to another process"
            ) from None

    async def open(self, profile_name: str) -> dict:
        if self.shutting_down or self.opening:
            raise VoiceError("server_busy", "server is opening or shutting down")
        if len(self.sessions) >= self.settings.limits.max_sessions:
            raise VoiceError("session_capacity", "close an existing session first")
        if profile_name not in self.settings.profiles:
            raise VoiceError("unknown_profile", "select a profile defined in the server config")
        profile = self.settings.profiles[profile_name]
        if profile.backend == "live" and any(
            s.profile.backend == "live" for s in self.sessions.values()
        ):
            raise VoiceError(
                "devices_in_use", "only one live audio session per process is supported"
            )
        self.opening = True
        session = Session(profile_name, profile, self.settings)
        self.sessions[session.id] = session
        try:
            await session.start()
            return session.state()
        except BaseException:
            with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
                await session.close("open_failed")
            self.sessions.pop(session.id, None)
            raise
        finally:
            self.opening = False

    async def close(self, session_id: str, reason: str = "closed") -> dict:
        session = self.sessions.get(session_id)
        if session is None:
            return {"session_id": session_id, "closed": True, "reason": "already_absent"}
        result = await session.close(reason)
        self.sessions.pop(session_id, None)
        return result

    def stop_all(self, reason: str) -> None:
        for session in self.sessions.values():
            session.stop_output(reason)

    async def expire(self) -> None:
        now = time.monotonic()
        for session in list(self.sessions.values()):
            session.record_playback()
            if now >= session.expires_at:
                await self.close(session.id, "lease_expired")

    async def close_all(self) -> None:
        self.shutting_down = True
        self.stop_all("client_disconnected")
        await asyncio.gather(
            *(self.close(sid, "client_disconnected") for sid in list(self.sessions)),
            return_exceptions=True,
        )
