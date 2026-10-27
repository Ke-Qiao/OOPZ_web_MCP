from __future__ import annotations

import asyncio
import threading
import time
from collections import deque


class Playback:
    """Bounded mono s16le PCM; the callback never waits for the control lock."""

    def __init__(self, sample_rate: int, frame_ms: int, queue_ms: int):
        self.sample_rate = sample_rate
        self.frame_bytes = sample_rate * frame_ms // 1000 * 2
        self.capacity = max(1, queue_ms // frame_ms)
        self.queue: deque[bytes] = deque()
        self.lock = threading.Lock()
        self.epoch = 0
        self.active = False
        self.producer_done = False
        self.voice_at = 0.0
        self.cleared_through = 0.0
        self.local_samples = 0
        self.first_output_at: float | None = None
        self.stop_reason = "idle"
        self.offset = 0

    @property
    def speech_pending(self) -> bool:
        return self.voice_at > self.cleared_through

    def voice_evidence(self, at: float) -> None:
        self.voice_at = max(self.voice_at, at)

    def release_speech(self, through: float) -> None:
        self.cleared_through = max(self.cleared_through, through)

    def begin(self) -> int:
        with self.lock:
            self.epoch += 1
            self.queue.clear()
            self.offset = 0
            self.active = True
            self.producer_done = False
            self.local_samples = 0
            self.first_output_at = None
            self.stop_reason = ""
            return self.epoch

    def stop(self, reason: str) -> int:
        with self.lock:
            self.epoch += 1
            self.active = False
            self.queue.clear()
            self.offset = 0
            self.stop_reason = reason
            return self.epoch

    def finish(self, epoch: int) -> None:
        with self.lock:
            if epoch == self.epoch:
                self.producer_done = True
                if not self.queue:
                    self.active = False

    async def enqueue(self, epoch: int, pcm: bytes) -> bool:
        if len(pcm) > self.frame_bytes or len(pcm) % 2:
            raise ValueError("PCM must be one frame or less, aligned to 16-bit samples")
        while epoch == self.epoch and self.active:
            with self.lock:
                if epoch != self.epoch or not self.active:
                    return False
                if len(self.queue) < self.capacity:
                    self.queue.append(pcm)
                    return True
            await asyncio.sleep(0.005)
        return False

    def render_into(self, outdata) -> None:
        # PortAudio callback: no model, network, logging, await, or blocking lock.
        outdata[:] = b"\0" * len(outdata)
        if not self.lock.acquire(blocking=False):
            return
        try:
            if not self.active or self.speech_pending:
                return
            epoch = self.epoch
            written = 0
            previous_offset = self.offset
            consumed = []
            while self.queue and written < len(outdata):
                pcm = self.queue[0]
                count = min(len(pcm) - self.offset, len(outdata) - written)
                outdata[written : written + count] = pcm[self.offset : self.offset + count]
                self.offset += count
                written += count
                if self.offset == len(pcm):
                    consumed.append(self.queue.popleft())
                    self.offset = 0
            if self.speech_pending or epoch != self.epoch:
                outdata[:] = b"\0" * len(outdata)
                self.queue.extendleft(reversed(consumed))
                self.offset = previous_offset
                return
            if written:
                if self.first_output_at is None:
                    self.first_output_at = time.monotonic()
                self.local_samples += written // 2
            if self.producer_done and not self.queue:
                self.active = False
        finally:
            self.lock.release()

    def snapshot(self) -> dict:
        return {
            "output_epoch": self.epoch,
            "active": self.active,
            "paused_for_speech": self.speech_pending,
            "queued_frames": len(self.queue),
            "queue_capacity_frames": self.capacity,
            "local_sample_position": self.local_samples,
            "sample_rate": self.sample_rate,
            "position_scope": "local_device_submission_not_remote_hearing",
            "last_stop_reason": self.stop_reason,
        }
