from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote, urlencode

from .config import ASRConfig, AudioConfig
from .playback import Playback

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

ENDPOINT = "wss://office-api-ast-dx.iflyaisol.com/ast/communicate/v1"
FRAME_BYTES = 1280
FRAME_SECONDS = 0.04


class ASRError(ValueError):
    """Only a sanitized error code may cross into MCP/logs, never the signed URL."""


@dataclass(frozen=True, repr=False)
class Credentials:
    app_id: str
    api_key: str
    api_secret: str


def load_credentials(config: ASRConfig) -> Credentials:
    if config.credentials_file:
        try:
            with Path(config.credentials_file).open("r", encoding="utf-8-sig") as source:
                content = source.read(16385)
        except (OSError, UnicodeError):
            raise ASRError("xfyun_credentials_unreadable") from None
        if len(content) > 16384:
            raise ASRError("xfyun_credentials_too_large")
        fields = {}
        for line in content.splitlines():
            match = re.fullmatch(r"\s*(APPID|APIKey|APISecret)\s*[:：]\s*(\S+)\s*", line, re.I)
            if match:
                name, value = match.groups()
                if name.lower() in fields:
                    raise ASRError("xfyun_duplicate_credential_field")
                fields[name.lower()] = value
        values = [fields.get(key, "") for key in ("appid", "apikey", "apisecret")]
    else:
        values = [
            os.environ.get(name, "")
            for name in (config.app_id_env, config.api_key_env, config.api_secret_env)
        ]
    if any(not value or any(c.isspace() for c in value) or len(value) > 512 for value in values):
        raise ASRError("xfyun_missing_or_invalid_credentials")
    return Credentials(*values)


def signed_url(
    config: ASRConfig,
    credentials: Credentials,
    *,
    now: datetime | None = None,
    request_uuid: str | None = None,
) -> str:
    current = now or datetime.now(timezone(timedelta(hours=8)))
    if current.tzinfo is None:
        raise ValueError("XFYun signing requires a timezone-aware time")
    params = {
        "accessKeyId": credentials.api_key,
        "appId": credentials.app_id,
        "uuid": request_uuid or uuid.uuid4().hex,
        "utc": current.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "audio_encode": "pcm_s16le",
        "lang": config.lang,
        "samplerate": "16000",
        "role_type": "0",
    }
    if config.domain:
        params["pd"] = config.domain
    canonical = urlencode(sorted(params.items()), quote_via=quote, safe="")
    signature = hmac.new(credentials.api_secret.encode(), canonical.encode(), hashlib.sha1).digest()
    params["signature"] = base64.b64encode(signature).decode("ascii")
    return ENDPOINT + "?" + urlencode(params, quote_via=quote, safe="")


def _integer(value, field: str) -> int:
    if isinstance(value, bool) or not re.fullmatch(r"\d{1,10}", str(value)):
        raise ASRError(f"xfyun_invalid_{field}")
    return int(value)


def parse_message(raw: str | bytes) -> dict:
    if len(raw) > 65536:
        raise ASRError("xfyun_message_too_large")
    try:
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ASRError("xfyun_invalid_message")
        data = message.get("data", {})
        if isinstance(data, str):
            data = json.loads(data) if data else {}
        if not isinstance(data, dict):
            raise ASRError("xfyun_invalid_data")
        code = message.get("code", data.get("code", 0))
        if str(code) not in {"0", "000000"}:
            safe_code = str(code) if re.fullmatch(r"\d{1,8}", str(code)) else "service_error"
            raise ASRError("xfyun_" + safe_code)
        if message.get("action") == "error" or data.get("normal") is False:
            raise ASRError("xfyun_service_error")
        if message.get("action") == "started" or message.get("msg_type") == "action":
            sid = data.get("sessionId") or message.get("sid")
            if not isinstance(sid, str) or not 1 <= len(sid) <= 128:
                raise ASRError("xfyun_missing_session_id")
            return {"kind": "ready", "session_id": sid}
        if message.get("res_type") == "frc":
            return {"kind": "status"}
        if message.get("action") != "result" and message.get("msg_type") != "result":
            raise ASRError("xfyun_unknown_message")
        if message.get("res_type", "asr") != "asr":
            raise ASRError("xfyun_unexpected_result_type")
        segment = data["cn"]["st"]
        result_type = str(segment["type"])
        if result_type not in {"0", "1"}:
            raise ASRError("xfyun_invalid_result_type")
        words = []
        for result in segment["rt"]:
            for word in result["ws"]:
                choice = word["cw"][0]
                if choice.get("wp") not in {"g", "s"}:
                    if not isinstance(choice["w"], str):
                        raise ASRError("xfyun_invalid_text")
                    words.append(choice["w"])
        text = "".join(words).strip()
        if len(text) > 32768:
            raise ASRError("xfyun_transcript_too_large")
        start_ms = _integer(segment["bg"], "start") if "bg" in segment else None
        end_ms = _integer(segment.get("ed", 0) if result_type == "1" else segment["ed"], "endpoint")
        if result_type == "0" and start_ms is not None and start_ms > end_ms:
            raise ASRError("xfyun_invalid_audio_timeline")
        return {
            "kind": "final" if result_type == "0" else "partial",
            "seg_id": _integer(data["seg_id"], "segment_id"),
            "text": text,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "stream_final": data.get("ls", False) is True,
        }
    except (
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        RecursionError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        raise ASRError("xfyun_invalid_message") from None


class XFYunASR:
    def __init__(
        self,
        config: ASRConfig,
        audio: AudioConfig,
        rx: queue.Queue,
        playback: Playback,
        emit: Callable[[dict], None],
        listening=None,
    ):
        self.config, self.audio, self.rx = config, audio, rx
        self.playback, self.emit = playback, emit
        self.listening = listening
        self.connection: ClientConnection | None = None
        self.session_id: str | None = None
        self.sent_frames = 0
        self.audio_ms = 0
        self.last_voice_ms = -1
        self.last_voice_at = 0.0
        self.last_capture_at = 0.0
        self.last_final_ms = -1
        self.last_final_id = -1
        self.last_final_digest = ""
        self.partial_open = False
        self.pending_since = 0.0
        self.input_open = False
        self.completion_needed = False
        self.closing = False
        self.tasks: list[asyncio.Task] = []

    async def open(self) -> None:
        from websockets.asyncio.client import connect

        # A dedicated disabled logger prevents debug output from leaking signed query strings.
        logger = logging.getLogger("oopz_mcp.xfyun.websocket")
        logger.disabled = True
        try:
            credentials = await asyncio.to_thread(load_credentials, self.config)
            self.connection = await connect(
                signed_url(self.config, credentials),
                proxy=None,
                compression=None,
                open_timeout=self.config.connect_timeout_seconds,
                close_timeout=1,
                max_size=65536,
                max_queue=8,
                write_limit=16384,
                logger=logger,
            )
            async with asyncio.timeout(self.config.connect_timeout_seconds):
                while self.session_id is None:
                    event = parse_message(await self.connection.recv())
                    if event["kind"] == "ready":
                        self.session_id = event["session_id"]
                    elif event["kind"] != "status":
                        raise ASRError("xfyun_expected_ready")
        except asyncio.CancelledError:
            raise
        except ASRError:
            raise
        except Exception:
            raise ASRError("xfyun_connection_failed") from None

    def _can_complete(self) -> bool:
        return (
            not self.partial_open
            and self.last_final_ms >= 0
            and self.last_final_ms > self.last_voice_ms
            and self.playback.voice_at <= self.last_capture_at
            and (self.last_capture_at - self.last_voice_at) * 1000 >= self.audio.endpoint_ms
        )

    def _complete_if_ready(self) -> None:
        if self.input_open and self._can_complete():
            self.emit(
                {
                    "type": "speech_end",
                    "through": self.last_capture_at,
                    "notify": self.completion_needed,
                }
            )
            self.input_open = False
            self.completion_needed = False

    async def _send(self) -> None:
        import numpy as np

        assert self.connection is not None
        pending = bytearray()
        next_send = 0.0
        while True:
            try:
                pcm, voiced, captured_at = self.rx.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.005)
                continue
            self.last_capture_at = captured_at
            if self.listening is not None:
                self.listening.feed_frame(
                    pcm, voiced, captured_at, self.audio_ms, self.audio_ms + self.audio.frame_ms
                )
            elif voiced:
                self.last_voice_at, self.last_voice_ms = captured_at, self.audio_ms
                self.pending_since = captured_at
                self.input_open = True
            self.audio_ms += self.audio.frame_ms
            pending.extend((np.clip(pcm, -1, 32767 / 32768) * 32768).astype("<i2").tobytes())
            if len(pending) >= FRAME_BYTES:
                await asyncio.sleep(max(0, next_send - time.monotonic()))
                sent_at = time.monotonic()
                if next_send and sent_at - next_send > FRAME_SECONDS:
                    raise ASRError("xfyun_audio_send_stalled")
                self.sent_frames += 1
                async with asyncio.timeout(2):
                    await self.connection.send(bytes(pending[:FRAME_BYTES]))
                del pending[:FRAME_BYTES]
                # Absolute cadence avoids accumulating scheduler jitter over long sessions.
                next_send = (next_send or sent_at) + FRAME_SECONDS
            if self.listening is None:
                self._complete_if_ready()
            if (
                self.listening is None
                and self.input_open
                and self.pending_since
                and time.monotonic() - self.pending_since > self.config.final_timeout_seconds
            ):
                raise ASRError("xfyun_final_timeout")

    async def _receive(self) -> None:
        assert self.connection is not None
        while True:
            event = parse_message(await self.connection.recv())
            kind = event["kind"]
            if kind not in {"final", "partial", "status"}:
                raise ASRError("xfyun_unexpected_message")
            if kind == "partial":
                if event["seg_id"] <= self.last_final_id:
                    raise ASRError("xfyun_result_out_of_order")
                if self.listening is None:
                    if not self.input_open:
                        self.pending_since = time.monotonic()
                    self.partial_open = True
                    self.input_open = True
                    self.playback.voice_evidence(time.monotonic())
            if kind == "final":
                digest = hashlib.sha256(
                    json.dumps(
                        [event["seg_id"], event.get("start_ms"), event["end_ms"], event["text"]],
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                if event["seg_id"] == self.last_final_id:
                    if digest != self.last_final_digest:
                        raise ASRError("xfyun_final_conflict")
                    if event.get("stream_final"):
                        raise ASRError("xfyun_stream_ended")
                    continue
                if event["seg_id"] < self.last_final_id or event["end_ms"] < self.last_final_ms:
                    raise ASRError("xfyun_result_out_of_order")
                if event["end_ms"] > self.sent_frames * 40:
                    raise ASRError("xfyun_invalid_audio_timeline")
                self.last_final_id, self.last_final_digest = event["seg_id"], digest
                self.last_final_ms = event["end_ms"]
                self.partial_open = False
                if self.listening is not None:
                    self.listening.feed_final(event)
                    if event.get("stream_final"):
                        raise ASRError("xfyun_stream_ended")
                    continue
                complete = self._can_complete()
                self.emit(
                    {
                        "type": "transcript",
                        "text": event["text"],
                        "final": complete,
                        "through": self.last_capture_at,
                        "last_voice_at": (
                            self.last_voice_at if self.last_voice_ms < self.last_final_ms else 0.0
                        ),
                        "endpoint_at": time.monotonic(),
                        "endpoint_metric": "asr_final_received",
                    }
                )
                if not complete and not self.input_open:
                    self.pending_since = time.monotonic()
                self.input_open = not complete
                self.completion_needed = not complete and (
                    self.completion_needed or bool(event["text"])
                )

            if event.get("stream_final"):
                raise ASRError("xfyun_stream_ended")

    async def run(self) -> None:
        self.tasks = [asyncio.create_task(self._send()), asyncio.create_task(self._receive())]
        try:
            done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
            if not self.closing:
                raise ASRError("xfyun_disconnected")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self.closing:
                code = str(exc) if isinstance(exc, ASRError) else "xfyun_disconnected"
                self.playback.stop(code)
                self.emit({"type": "fault", "code": code})
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def close(self) -> None:
        self.closing = True
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.connection:
            if self.sent_frames and self.session_id:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(1):
                        await self.connection.send(
                            json.dumps({"end": True, "sessionId": self.session_id})
                        )
            with contextlib.suppress(Exception):
                async with asyncio.timeout(2):
                    await self.connection.close()
            self.connection = None
