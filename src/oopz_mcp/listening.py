from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from .config import Profile
from .playback import Playback
from .speech_models import CamPlus, SmartTurn, SpeakerRegistry


@dataclass
class AudioSlice:
    start: int
    end: int
    at: float
    pcm: np.ndarray
    voiced: bool
    speaker: str | None = None
    epoch: int = 0


@dataclass
class SpeechTurn:
    epoch: int
    speaker: str
    last_voice_ms: int
    last_voice_at: float
    final_through: int = -1
    delivered: bool = False
    closed: bool = False
    superseded: bool = False
    held: list[dict] = field(default_factory=list)
    decision_at: int = -1
    probability: float | None = None
    verification_through: int = 0


class ListeningController:
    """Audio-ordered speech with identity filtering enabled by default."""

    def __init__(self, profile: Profile, playback: Playback, emit, encoder, registry, detector):
        assert profile.audio and profile.speakers and profile.turn and profile.asr
        self.profile, self.playback, self.emit = profile, playback, emit
        self.audio, self.speakers, self.turn_config = profile.audio, profile.speakers, profile.turn
        self.asr_timeout_seconds = profile.asr.final_timeout_seconds
        self.encoder, self.registry, self.detector = encoder, registry, detector
        capacity = (profile.turn.inference_timeout_seconds * 1000 + 4000) // self.audio.frame_ms
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=capacity + 64)
        self.history: deque[AudioSlice] = deque(maxlen=120000 // self.audio.frame_ms)
        self.block: list[AudioSlice] = []
        self.finals: deque[dict] = deque()
        self.turns: deque[SpeechTurn] = deque(maxlen=128)
        self.active: SpeechTurn | None = None
        self.epoch = 0
        self.processed_ms = 0
        self.received_voice_ms = 0
        self.closed = False
        self.failed = False
        self.task: asyncio.Task | None = None
        self.filtered_segments = 0
        self.uncertain_turns = 0

    @classmethod
    def prepare(cls, profile, playback, emit):
        encoder = registry = None
        if profile.speakers.enabled:
            encoder = CamPlus(profile.speakers)
            registry = SpeakerRegistry(
                profile.speakers.registry_path, encoder.fingerprint, encoder.dimension
            ).load()
            if not registry.list():
                raise ValueError("speaker_registry_empty_enroll_before_voice_open")
        detector = SmartTurn(profile.turn)
        return cls(profile, playback, emit, encoder, registry, detector)

    def start(self):
        self.task = asyncio.create_task(self.run())

    def _put(self, item):
        if self.closed or self.failed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self._fault("listening_input_gap")

    def feed_frame(self, pcm, voiced, at, start_ms, end_ms):
        if voiced:
            self.received_voice_ms = end_ms
        self._put(("frame", AudioSlice(start_ms, end_ms, at, pcm, voiced)))

    def feed_final(self, event):
        self._put(("final", dict(event, endpoint_at=time.monotonic())))

    def _fault(self, code):
        if not self.closed and not self.failed:
            self.failed = True
            self.playback.stop(code)
            self.emit({"type": "fault", "code": code})

    async def _infer(self, function, *args):
        async with asyncio.timeout(self.turn_config.inference_timeout_seconds):
            return await asyncio.to_thread(function, *args)

    async def _classify(self):
        block, self.block = self.block, []
        voiced_ms = sum(f.end - f.start for f in block if f.voiced)
        speaker_id = None
        last = next((f for f in reversed(block) if f.voiced), None)
        previous_voice = next((f for f in reversed(self.history) if f.voiced), None)
        # shortcut: 200 ms is the model input floor; short-tail accuracy needs live calibration.
        short_tail = (
            200 <= voiced_ms < self.speakers.min_speech_ms
            and self.active is not None
            and not self.active.closed
            and previous_voice is not None
            and self.history[-1].end == block[0].start
            and previous_voice.epoch == self.active.epoch
            and block[0].start - self.active.last_voice_ms < self.turn_config.silence_ms
        )
        if not self.speakers.enabled and last is not None:
            speaker_id = "unverified"
        elif self.speakers.enabled and (voiced_ms >= self.speakers.min_speech_ms or short_tail):
            assert last is not None
            # The tail must identify itself; earlier verified PCM could hide a new voice.
            embedding = await self._infer(
                self.encoder.embedding, np.concatenate([f.pcm for f in block if f.end <= last.end])
            )
            if self.closed or self.failed:
                return
            match = self.registry.identify(embedding, self.speakers.threshold, self.speakers.margin)
            if short_tail and match and self.active and match.speaker_id != self.active.speaker:
                match = None
            speaker_id = match.speaker_id if match else None
        if speaker_id:
            assert last is not None
            if self.active is None or self.active.closed or self.active.speaker != speaker_id:
                if self.active and not self.active.closed:
                    self.active.superseded = True
                    self._finish_superseded(self.active)
                self.epoch += 1
                self.active = SpeechTurn(self.epoch, speaker_id, last.end, last.at)
                self.turns.append(self.active)
                self.emit(
                    {
                        "type": "speaker_activity",
                        "speaker_id": speaker_id,
                        "speech_epoch": self.epoch,
                    }
                )
            self.active.last_voice_ms, self.active.last_voice_at = last.end, last.at
            self.active.probability = None
            self.playback.voice_evidence(last.at)
            for frame in block:
                frame.speaker = speaker_id
                frame.epoch = self.active.epoch
        self.history.extend(block)
        self.processed_ms = block[-1].end

    async def _frame(self, frame):
        if self.block or frame.voiced:
            self.block.append(frame)
            last_voice = next((f.end for f in reversed(self.block) if f.voiced), frame.end)
            duration = frame.end - self.block[0].start
            voiced_ms = sum(f.end - f.start for f in self.block if f.voiced)
            if (
                not self.speakers.enabled
                or (
                    duration >= self.speakers.window_ms and voiced_ms >= self.speakers.min_speech_ms
                )
                or duration >= 3000
                or frame.end - last_voice >= self.turn_config.silence_ms
            ):
                await self._classify()
        else:
            self.history.append(frame)
            self.processed_ms = frame.end

    def _uncertain(self, turn, reason):
        if turn.closed:
            return
        turn.closed = True
        turn.held.clear()
        self.uncertain_turns += 1
        self.emit(
            {
                "type": "input_uncertain",
                "speaker_id": turn.speaker,
                "speech_epoch": turn.epoch,
                "reason": reason,
            }
        )
        if turn is self.active:
            self.playback.release_speech(turn.last_voice_at)

    async def _verify_final(self, frames, start, end, speaker):
        if not self.speakers.enabled:
            return speaker == "unverified" and any(f.voiced for f in frames)
        # Block identity is not evidence for a shorter ASR segment inside that block.
        voiced_ms = sum(min(f.end, end) - max(f.start, start) for f in frames if f.voiced)
        if voiced_ms < self.speakers.min_speech_ms:
            return False
        pcm = np.concatenate(
            [
                f.pcm[(max(start, f.start) - f.start) * 16 : (min(end, f.end) - f.start) * 16]
                for f in frames
            ]
        )
        maximum = 15 * 16000
        minimum = self.speakers.min_speech_ms * 16
        for offset in range(0, pcm.size, maximum):
            stop = min(offset + maximum, pcm.size)
            begin = min(offset, max(0, stop - minimum))
            embedding = await self._infer(self.encoder.embedding, pcm[begin:stop])
            if self.closed or self.failed:
                return False
            match = self.registry.identify(embedding, self.speakers.threshold, self.speakers.margin)
            if match is None or match.speaker_id != speaker:
                return False
        return True

    async def _drain_finals(self):
        while self.finals and self.finals[0]["end_ms"] <= self.processed_ms:
            event = self.finals.popleft()
            start, end = event.get("start_ms"), event["end_ms"]
            frames = [
                f for f in self.history if start is not None and f.end > start and f.start < end
            ]
            voices = [f for f in frames if f.voiced]
            epochs = {f.epoch for f in voices}
            valid = (
                start is not None
                and self.history
                and start >= self.history[0].start
                and voices
                and len(epochs) == 1
                and 0 not in epochs
            )
            for turn in self.turns:
                if turn.epoch in epochs:
                    turn.final_through = max(turn.final_through, end)
            if not valid:
                self.filtered_segments += 1
                for turn in self.turns:
                    if turn.epoch in epochs:
                        self._uncertain(turn, "mixed_or_unverified_audio")
                continue
            epoch = next(iter(epochs))
            turn = next((t for t in self.turns if t.epoch == epoch), None)
            if turn is None or turn.closed:
                self.filtered_segments += 1
                continue
            text = event["text"].strip()
            if not text.strip("。，！？,.!? "):
                self._finish_superseded(turn)
                continue
            if not await self._verify_final(frames, start, end, turn.speaker):
                if self.closed or self.failed:
                    return
                self.filtered_segments += 1
                self._uncertain(turn, "final_speaker_unverified")
                continue
            data = {
                "type": "transcript",
                "text": text,
                "final": False,
                "speaker_id": turn.speaker,
                "speech_epoch": turn.epoch,
                "through": turn.last_voice_at,
                "last_voice_at": voices[-1].at,
                "endpoint_at": event["endpoint_at"],
                "endpoint_metric": "asr_final_received",
            }
            normalized = text.strip("。，！？,.!? ").casefold()
            if not turn.delivered and normalized in {
                x.casefold() for x in self.profile.backchannels
            }:
                turn.held.append(data)
            else:
                for held in turn.held:
                    self.emit(held)
                turn.held.clear()
                self.emit(data)
                turn.delivered = True
            self._finish_superseded(turn)

    def _finish_superseded(self, turn):
        if turn.superseded and not turn.closed and turn.final_through >= turn.last_voice_ms:
            for held in turn.held:
                self.emit(held)
            turn.held.clear()
            turn.closed = True
            self.emit(
                {
                    "type": "speech_superseded",
                    "speaker_id": turn.speaker,
                    "speech_epoch": turn.epoch,
                    "reason": "speaker_change",
                }
            )

    async def _complete(self):
        turn = self.active
        if not turn or turn.closed or not self.history:
            return
        silence = self.processed_ms - turn.last_voice_ms
        if turn.final_through < turn.last_voice_ms:
            if silence > self.asr_timeout_seconds * 1000:
                self._fault("xfyun_final_timeout")
            return
        if silence < self.turn_config.silence_ms:
            return
        if turn.probability is None:
            frames = [
                f
                for f in self.history
                if f.end > turn.last_voice_ms - 8000
                and f.end <= turn.last_voice_ms
                and f.epoch == turn.epoch
            ]
            if not frames:
                self._uncertain(turn, "turn_audio_expired")
                return
            # Keep time gaps; never splice another speaker into this speaker's context.
            first = frames[0].start
            pcm = np.zeros((turn.last_voice_ms - first) * 16, dtype="float32")
            for f in frames:
                pcm[(f.start - first) * 16 : (f.end - first) * 16] = f.pcm
            cutoff = turn.last_voice_ms
            probability = await self._infer(self.detector.probability, pcm)
            if self.closed or self.failed:
                return
            turn.decision_at, turn.probability = cutoff, probability
            # A fixed fence lets newly rejected speech stop delaying an earlier turn.
            turn.verification_through = max(self.received_voice_ms, self.processed_ms)
        if self.processed_ms < turn.verification_through or turn is not self.active:
            return
        assert turn.probability is not None
        if turn.probability < self.turn_config.threshold:
            if silence >= self.turn_config.max_wait_seconds * 1000:
                self._uncertain(turn, "semantic_end_uncertain")
            return
        for held in turn.held:
            self.emit(dict(held, final=True))
        turn.held.clear()
        turn.closed = True
        self.emit(
            {
                "type": "speech_end",
                "through": turn.last_voice_at,
                "notify": turn.delivered,
                "speaker_id": turn.speaker,
                "speech_epoch": turn.epoch,
                "completion_reason": "smart_turn",
                "completion_probability": turn.probability,
            }
        )

    async def run(self):
        try:
            while not self.closed and not self.failed:
                kind, value = await self.queue.get()
                if kind == "frame":
                    await self._frame(value)
                else:
                    if len(self.finals) >= 64:
                        self._fault("listening_result_gap")
                        return
                    self.finals.append(value)
                if self.closed or self.failed:
                    return
                await self._drain_finals()
                if self.closed or self.failed:
                    return
                for turn in self.turns:
                    if (
                        turn.superseded
                        and not turn.closed
                        and self.processed_ms - turn.last_voice_ms > self.asr_timeout_seconds * 1000
                    ):
                        self._uncertain(turn, "asr_result_missing_after_speaker_change")
                await self._complete()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fault("listening_inference_failed")

    def snapshot(self):
        return {
            "speaker_verification_enabled": self.speakers.enabled,
            "enrolled_speakers": len(self.registry.list()) if self.registry is not None else 0,
            "speaker_model": "cam++" if self.speakers.enabled else None,
            "turn_model": "smart-turn-v3.2",
            "active_speaker_id": self.active.speaker if self.active else None,
            "speech_epoch": self.active.epoch if self.active else 0,
            "filtered_segments": self.filtered_segments,
            "uncertain_turns": self.uncertain_turns,
        }

    async def close(self):
        self.closed = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.block.clear()
        self.history.clear()
        self.finals.clear()
        self.turns.clear()
        while not self.queue.empty():
            self.queue.get_nowait()
