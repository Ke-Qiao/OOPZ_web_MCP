from __future__ import annotations

import asyncio
import contextlib
import os
import queue
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from typing import TYPE_CHECKING

import anyio
import httpx

from .config import Limits, Profile
from .playback import Playback
from .xfyun import XFYunASR

if TYPE_CHECKING:
    from .listening import ListeningController


class MockBackend:
    """No audio device, network, or model. Injection is internal, never an MCP tool."""

    def __init__(self, profile: Profile, limits: Limits):
        self.profile = profile
        self.playback = Playback(16000, 20, limits.output_queue_ms)
        self.emit: Callable[[dict], None] = lambda event: None
        self.task: asyncio.Task | None = None
        self.synthesis_count = 0

    async def start(self, emit: Callable[[dict], None]) -> dict:
        self.emit = emit
        self.task = asyncio.create_task(self._pump())
        return {"backend": "mock", "audio_devices_open": False, "tts": "synthetic_silence"}

    async def _pump(self) -> None:
        buffer = bytearray(self.playback.frame_bytes)
        started = time.monotonic()
        pending = deque(self.profile.mock_turns)
        while True:
            while pending and (time.monotonic() - started) * 1000 >= pending[0].after_ms:
                self.inject(pending.popleft().text)
            self.playback.render_into(buffer)
            await asyncio.sleep(0.02)

    def inject(self, text: str, *, final: bool = True) -> None:
        now = time.monotonic()
        self.playback.voice_evidence(now)
        self.emit(
            {
                "type": "transcript",
                "text": text,
                "final": final,
                "through": now,
                "last_voice_at": now,
                "endpoint_at": now,
            }
        )

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        self.synthesis_count += 1
        for _ in range(max(1, min(len(text), 50))):
            await asyncio.sleep(0.01)
            yield b"\0" * self.playback.frame_bytes

    async def close(self) -> None:
        self.playback.stop("backend_closed")
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task


class LiveBackend:
    """PortAudio RX/TX, XFYun realtime ASR, configured GPT-SoVITS HTTP API."""

    def __init__(self, profile: Profile, limits: Limits):
        assert profile.audio is not None and profile.asr is not None and profile.tts is not None
        self.profile = profile
        self.audio = profile.audio
        self.asr = profile.asr
        self.tts = profile.tts
        self.limits = limits
        self.playback = Playback(
            self.audio.tx_sample_rate, self.audio.frame_ms, limits.output_queue_ms
        )
        self.render_pcm = bytearray(self.playback.frame_bytes)
        self.rx: queue.Queue = queue.Queue(maxsize=limits.input_queue_ms // self.audio.frame_ms)
        self.input_stream = None
        self.output_stream = None
        self.recognition: XFYunASR | None = None
        self.listening: ListeningController | None = None
        self.asr_task: asyncio.Task | None = None
        self.pump_task: asyncio.Task | None = None
        self.client: httpx.AsyncClient | None = None
        self.emit: Callable[[dict], None] = lambda event: None
        self.rx_dropped = 0
        self.tx_status_count = 0
        self.callback_error = False
        self.last_callback_at = time.monotonic()

    def _prepare(self) -> None:
        import numpy as np
        import sounddevice as sd

        self.np = np
        if self.profile.speakers:
            from .listening import ListeningController

            self.listening = ListeningController.prepare(self.profile, self.playback, self.emit)
        devices = sd.query_devices()

        def exact_device(name: str, channel_key: str, channels: int) -> int:
            found = [
                i
                for i, device in enumerate(devices)
                if device["name"] == name and device[channel_key] >= channels
            ]
            if len(found) != 1:
                raise ValueError("device name must match exactly one capable audio device")
            return found[0]

        rx = exact_device(self.audio.rx_device, "max_input_channels", self.audio.rx_channels)
        tx = exact_device(self.audio.tx_device, "max_output_channels", self.audio.tx_channels)
        if rx == tx:
            raise ValueError("RX/TX resolve to the same physical audio endpoint")
        sd.check_input_settings(
            device=rx,
            channels=self.audio.rx_channels,
            samplerate=self.audio.rx_sample_rate,
            dtype="float32",
        )
        sd.check_output_settings(
            device=tx,
            channels=self.audio.tx_channels,
            samplerate=self.audio.tx_sample_rate,
            dtype="int16",
        )
        self.input_stream = sd.InputStream(
            device=rx,
            channels=self.audio.rx_channels,
            samplerate=self.audio.rx_sample_rate,
            dtype="float32",
            blocksize=self.audio.rx_sample_rate * self.audio.frame_ms // 1000,
            callback=self._capture,
            latency="low",
        )
        self.output_stream = sd.RawOutputStream(
            device=tx,
            channels=self.audio.tx_channels,
            samplerate=self.audio.tx_sample_rate,
            dtype="int16",
            blocksize=self.audio.tx_sample_rate * self.audio.frame_ms // 1000,
            callback=self._render,
            latency="low",
        )

    async def start(self, emit: Callable[[dict], None]) -> dict:
        self.emit = emit
        # Shield setup so cancellation cannot orphan a late-created device.
        setup = asyncio.create_task(asyncio.to_thread(self._prepare))
        try:
            await asyncio.shield(setup)
            token = os.environ.get(self.tts.bearer_token_env) if self.tts.bearer_token_env else None
            if self.tts.bearer_token_env and not token:
                raise ValueError("configured TTS token environment variable is absent")
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.tts.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
                headers={"Authorization": f"Bearer {token}"} if token else {},
            )
            self.recognition = XFYunASR(
                self.asr, self.audio, self.rx, self.playback, emit, self.listening
            )
            await self.recognition.open()
            if self.listening:
                self.listening.start()
            assert self.output_stream is not None and self.input_stream is not None
            self.output_stream.start()
            self.input_stream.start()
            self.last_callback_at = time.monotonic()
            self.asr_task = asyncio.create_task(self.recognition.run())
            self.pump_task = asyncio.create_task(self._pump())
        except BaseException:
            self.playback.stop("open_failed_or_cancelled")
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(Exception):
                    await setup
                await self.close()
            raise
        return {
            "backend": "live",
            "audio_devices_open": True,
            "asr_provider": self.asr.provider,
            "asr_connection": "session_ready",
            "asr_audio_destination": "office-api-ast-dx.iflyaisol.com",
            "tts_model_configured": self.tts.model_id,
            "tts_api_revision_configured": self.tts.api_revision,
            "tts_connectivity": "not_probed",
            "tts_inference_warmup": "external_service_owned",
            "listening": self.listening.snapshot() if self.listening else {"enabled": False},
        }

    def _capture(self, indata, frames, timing, status) -> None:
        try:
            now = time.monotonic()
            self.last_callback_at = now
            pcm = indata.mean(axis=1) if self.audio.rx_channels == 2 else indata[:, 0].copy()
            # shortcut: energy VAD may pause on noise; upgrade after acoustic calibration.
            voiced = float(self.np.sqrt(self.np.mean(pcm * pcm))) >= self.audio.vad_rms
            if voiced and self.listening is None:
                self.playback.voice_evidence(now)
            if status:
                self.rx_dropped += 1
                self.playback.voice_evidence(now)
            try:
                self.rx.put_nowait((pcm, voiced, now))
            except queue.Full:
                self.rx_dropped += 1
                self.playback.voice_evidence(now)
        except Exception:
            self.callback_error = True
            self.playback.voice_evidence(time.monotonic())

    def _render(self, outdata, frames, timing, status) -> None:
        try:
            if status:
                self.tx_status_count += 1
            if self.audio.tx_channels == 1:
                self.playback.render_into(outdata)
            else:
                if len(outdata) != len(self.render_pcm) * 2:
                    raise ValueError("unexpected stereo output frame size")
                self.playback.render_into(self.render_pcm)
                stereo = self.np.frombuffer(outdata, dtype="<i2").reshape(-1, 2)
                mono = self.np.frombuffer(self.render_pcm, dtype="<i2")
                stereo[:, 0] = mono
                stereo[:, 1] = mono
        except Exception:
            outdata[:] = b"\0" * len(outdata)
            self.callback_error = True
            self.playback.voice_evidence(time.monotonic())

    async def _pump(self) -> None:
        dropped = 0
        output_flags = 0
        while True:
            if self.rx_dropped != dropped:
                dropped = self.rx_dropped
                self.emit({"type": "fault", "code": "audio_input_gap", "dropped": dropped})
                return
            if self.tx_status_count != output_flags:
                output_flags = self.tx_status_count
                self.emit({"type": "audio_status", "output_status_count": output_flags})
            if self.callback_error:
                self.emit({"type": "fault", "code": "audio_callback_failed"})
                return
            if time.monotonic() - self.last_callback_at > 2:
                self.emit({"type": "fault", "code": "audio_device_stalled"})
                return
            await asyncio.sleep(0.01)

    async def synthesize(self, text: str) -> AsyncGenerator[bytes, None]:
        assert self.client is not None
        payload = {
            "text": text,
            "text_lang": self.tts.text_lang,
            "ref_audio_path": self.tts.ref_audio_path,
            "prompt_text": self.tts.prompt_text,
            "prompt_lang": self.tts.prompt_lang,
            "media_type": "raw",
            "streaming_mode": self.tts.streaming_mode,
            "text_split_method": "cut5",
            "batch_size": 1,
        }
        if self.tts.gpt_streaming:
            payload["gpt_streaming"] = True
        async with self.client.stream("POST", self.tts.endpoint, json=payload) as response:
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            if media_type not in {"audio/raw", "application/octet-stream", "audio/pcm"}:
                raise ValueError("TTS response is not configured raw PCM")
            pending = b""
            total = 0
            limit = self.tts.sample_rate * 2 * self.limits.max_output_seconds
            async for data in response.aiter_bytes(chunk_size=self.playback.frame_bytes):
                total += len(data)
                if total > limit:
                    raise ValueError("TTS audio exceeds configured duration budget")
                pending += data
                while len(pending) >= self.playback.frame_bytes:
                    yield pending[: self.playback.frame_bytes]
                    pending = pending[self.playback.frame_bytes :]
            if len(pending) % 2:
                raise ValueError("truncated 16-bit PCM from TTS")
            if pending:
                yield pending
            if total == 0:
                raise ValueError("empty TTS stream")

    async def close(self) -> None:
        self.playback.stop("backend_closed")
        for task in (self.pump_task, self.asr_task):
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.recognition:
            await self.recognition.close()
        if self.listening:
            await self.listening.close()
        for stream in (self.input_stream, self.output_stream):
            if stream is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(stream.abort)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(stream.close)
        if self.client:
            await self.client.aclose()


def make_backend(profile: Profile, limits: Limits) -> MockBackend | LiveBackend:
    return (
        MockBackend(profile, limits) if profile.backend == "mock" else LiveBackend(profile, limits)
    )
