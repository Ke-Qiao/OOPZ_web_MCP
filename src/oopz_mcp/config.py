from __future__ import annotations

import tomllib
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[\w.:-]+$")]


class Reply(StrictModel):
    reply_id: Identifier
    reply_to_turn_id: Identifier | None = None
    based_on_input_revision: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=8000)

    @model_validator(mode="after")
    def nonempty(self) -> Reply:
        if not self.text.strip():
            raise ValueError("reply.text must contain non-whitespace text")
        return self


class Exchange(StrictModel):
    session_id: Identifier
    request_id: Identifier
    ack_event_seq: int = Field(ge=0)
    reply: Reply | None = None
    wait_ms: int = Field(default=15000, ge=250, le=60000)


class Limits(StrictModel):
    max_wait_ms: int = Field(default=30000, ge=250, le=60000)
    lease_seconds: int = Field(default=120, ge=10, le=3600)
    event_capacity: int = Field(default=32, ge=2, le=256)
    request_capacity: int = Field(default=256, ge=4, le=4096)
    max_sessions: int = Field(default=1, ge=1, le=8)
    transcript_chars: int = Field(default=2000, ge=64, le=8000)
    output_queue_ms: int = Field(default=80, ge=20, le=1000)
    input_queue_ms: int = Field(default=1000, ge=100, le=5000)
    max_output_seconds: int = Field(default=180, ge=1, le=600)

    @model_validator(mode="after")
    def lease_exceeds_wait(self) -> Limits:
        if self.lease_seconds * 1000 <= self.max_wait_ms + 5000:
            raise ValueError("lease_seconds must exceed max_wait_ms by more than 5 seconds")
        return self


class AudioConfig(StrictModel):
    rx_device: str = Field(min_length=1)
    tx_device: str = Field(min_length=1)
    rx_sample_rate: int = Field(default=16000, ge=8000, le=48000)
    tx_sample_rate: int = Field(default=32000, ge=8000, le=48000)
    rx_channels: Literal[1, 2] = 1
    tx_channels: Literal[1, 2] = 1
    frame_ms: Literal[10, 20] = 20
    vad_rms: float = Field(default=0.015, gt=0, lt=1)
    endpoint_ms: int = Field(default=320, ge=150, le=1500)

    @model_validator(mode="after")
    def separate_devices(self) -> AudioConfig:
        if self.rx_device.strip().casefold() == self.tx_device.strip().casefold():
            raise ValueError("RX and TX must be distinct explicitly named devices")
        if any(rate * self.frame_ms % 1000 for rate in (self.rx_sample_rate, self.tx_sample_rate)):
            raise ValueError("sample rates must give whole samples per frame")
        return self


class ASRConfig(StrictModel):
    provider: Literal["xfyun_rtasr_llm"] = "xfyun_rtasr_llm"
    credentials_file: str | None = Field(default=None, min_length=1, max_length=4096)
    app_id_env: str = Field(default="XFYUN_APP_ID", min_length=1, max_length=128)
    api_key_env: str = Field(default="XFYUN_API_KEY", min_length=1, max_length=128)
    api_secret_env: str = Field(default="XFYUN_API_SECRET", min_length=1, max_length=128)
    lang: Literal["autodialect", "autominor"] = "autodialect"
    domain: str | None = Field(default=None, min_length=1, max_length=64)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=30)
    final_timeout_seconds: int = Field(default=15, ge=2, le=60)


class TTSConfig(StrictModel):
    endpoint: str = Field(min_length=1)
    api_revision: str = Field(min_length=7)
    model_id: str = Field(min_length=1)
    ref_audio_path: str = Field(min_length=1)
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    sample_rate: int = Field(default=32000, ge=8000, le=48000)
    streaming_mode: Literal[2, 3] = 3
    gpt_streaming: bool = False
    bearer_token_env: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @model_validator(mode="after")
    def validate_endpoint(self) -> TTSConfig:
        url = urlsplit(self.endpoint)
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise ValueError("TTS endpoint must be a URL without credentials, query, or fragment")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("TTS endpoint requires HTTPS, except on loopback")
        if not url.path.endswith("/tts"):
            raise ValueError("provide the complete existing GPT-SoVITS /tts endpoint")
        return self


class MockTurn(StrictModel):
    after_ms: int = Field(ge=0, le=3600000)
    text: str = Field(min_length=1, max_length=8000)


ModelHash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class SpeakerConfig(StrictModel):
    enabled: bool = True
    model_path: str = Field(min_length=1)
    model_sha256: ModelHash
    registry_path: str = Field(default=".runtime/speakers/registry.json", min_length=1)
    threshold: float = Field(default=0.65, gt=0, lt=1)
    margin: float = Field(default=0.10, gt=0, lt=1)
    window_ms: int = Field(default=2000, ge=500, le=3000, multiple_of=100)
    min_speech_ms: int = Field(default=600, ge=200, le=3000)

    @model_validator(mode="after")
    def valid_window(self) -> SpeakerConfig:
        if self.min_speech_ms > self.window_ms:
            raise ValueError("min_speech_ms cannot exceed window_ms")
        return self


class TurnConfig(StrictModel):
    model_path: str = Field(min_length=1)
    model_sha256: ModelHash
    threshold: float = Field(default=0.5, gt=0, lt=1)
    silence_ms: int = Field(default=500, ge=200, le=2000)
    max_wait_seconds: int = Field(default=8, ge=2, le=30)
    inference_timeout_seconds: int = Field(default=5, ge=1, le=15)


class InstantConfig(StrictModel):
    endpoint: str = Field(default="http://127.0.0.1:3425/v1/chat/completions", max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    bearer_token_env: str | None = Field(default=None, min_length=1, max_length=128)
    system_prompt: str = Field(default="", max_length=8000)
    timeout_seconds: int = Field(default=8, ge=1, le=60)
    max_tokens: int = Field(default=384, ge=64, le=2048)

    @model_validator(mode="after")
    def local_endpoint(self) -> InstantConfig:
        try:
            url = urlsplit(self.endpoint)
            loopback = url.hostname == "localhost" or ip_address(url.hostname or "").is_loopback
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError
        except ValueError:
            raise ValueError("Instant endpoint requires a loopback host and valid port") from None
        if (
            not loopback
            or url.scheme not in {"http", "https"}
            or url.username is not None
            or url.password is not None
            or "?" in self.endpoint
            or "#" in self.endpoint
            or "\\" in self.endpoint
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.endpoint)
            or url.path != "/v1/chat/completions"
        ):
            raise ValueError("Instant endpoint must be a local /v1/chat/completions URL")
        if not self.model.strip():
            raise ValueError("Instant model must contain non-whitespace text")
        return self


class Profile(StrictModel):
    backend: Literal["mock", "live"] = "mock"
    audio: AudioConfig | None = None
    asr: ASRConfig | None = None
    tts: TTSConfig | None = None
    speakers: SpeakerConfig | None = None
    turn: TurnConfig | None = None
    instant: InstantConfig | None = None
    backchannels: list[str] = Field(default_factory=lambda: ["嗯", "嗯嗯", "哦", "哦哦"])
    mock_turns: list[MockTurn] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def complete_live(self) -> Profile:
        if (self.speakers is None) != (self.turn is None):
            raise ValueError("speakers and turn must be configured together")
        if self.instant and (self.backend != "live" or self.speakers is None or self.turn is None):
            raise ValueError("Instant requires a live profile with speakers and turn configured")
        if self.backend == "live":
            if self.mock_turns:
                raise ValueError("live profiles cannot inject mock turns")
            if self.audio is None or self.asr is None or self.tts is None:
                raise ValueError("live profile requires audio, asr and tts sections")
            if self.audio.tx_sample_rate != self.tts.sample_rate:
                raise ValueError("TX sample rate must match raw TTS PCM sample rate")
            if self.audio.rx_sample_rate != 16000:
                raise ValueError("XFYun RTASR requires 16000 Hz RX in this adapter")
        if self.backend == "mock" and any(
            (self.audio, self.asr, self.tts, self.speakers, self.turn)
        ):
            raise ValueError("mock profile does not accept real audio or model configuration")
        if self.mock_turns != sorted(self.mock_turns, key=lambda turn: turn.after_ms):
            raise ValueError("mock_turns must be ordered by after_ms")
        return self


class Settings(StrictModel):
    limits: Limits = Field(default_factory=Limits)
    profiles: dict[str, Profile] = Field(default_factory=lambda: {"mock": Profile()})
    local_stop_file: str = ".runtime/stop"
    log_metrics: bool = False


def load_settings(path: Path | None) -> Settings:
    if path is None:
        settings = Settings()
        root = Path.cwd()
    else:
        with path.open("rb") as source:
            settings = Settings.model_validate(tomllib.load(source))
        root = path.resolve().parent
    settings.local_stop_file = str((root / settings.local_stop_file).resolve())
    for profile in settings.profiles.values():
        if profile.asr and profile.asr.credentials_file:
            profile.asr.credentials_file = str((root / profile.asr.credentials_file).resolve())
        if profile.speakers:
            profile.speakers.model_path = str((root / profile.speakers.model_path).resolve())
            profile.speakers.registry_path = str((root / profile.speakers.registry_path).resolve())
        if profile.turn:
            profile.turn.model_path = str((root / profile.turn.model_path).resolve())
    return settings
