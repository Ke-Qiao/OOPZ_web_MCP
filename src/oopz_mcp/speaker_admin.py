"""Explicit local WAV or prompted RX enrollment; never calls a network service."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from .config import load_settings
from .speech_models import SAMPLE_RATE, CamPlus, SpeakerRegistry, SpeechModelError

if TYPE_CHECKING:
    from .config import AudioConfig


def _quality(pcm: np.ndarray) -> np.ndarray:
    if (
        pcm.dtype != np.float32
        or pcm.ndim != 1
        or not 2 * SAMPLE_RATE <= pcm.size <= 15 * SAMPLE_RATE
        or not np.isfinite(pcm).all()
        or np.max(np.abs(pcm)) > 1
    ):
        raise SpeechModelError("invalid_enrollment_audio")
    if float(np.mean(np.abs(pcm) >= 0.999)) > 0.01:
        raise SpeechModelError("enrollment_audio_clipped")
    levels = np.sqrt(np.mean(pcm[: pcm.size // 320 * 320].reshape(-1, 320) ** 2, axis=1))
    if np.count_nonzero(levels >= 0.01) * 0.02 < 1.5:
        raise SpeechModelError("enrollment_insufficient_voiced_audio")
    return pcm


def read_enrollment_wav(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as source:
            if (
                source.getframerate() != SAMPLE_RATE
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getcomptype() != "NONE"
                or not 2 * SAMPLE_RATE <= source.getnframes() <= 15 * SAMPLE_RATE
            ):
                raise SpeechModelError("enrollment_requires_2_to_15s_16khz_mono_s16_wav")
            frames = source.getnframes()
            raw = source.readframes(frames)
        if len(raw) != frames * 2:
            raise SpeechModelError("truncated_enrollment_audio")
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        return _quality(pcm)
    except (OSError, EOFError, wave.Error):
        raise SpeechModelError("enrollment_wav_unavailable_or_invalid") from None


def record_enrollment(audio: AudioConfig) -> list[np.ndarray]:
    """Capture only after explicit --record and a separate Enter for every sample."""
    if audio.rx_sample_rate != SAMPLE_RATE:
        raise SpeechModelError("enrollment_requires_16khz_rx")
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        matches = [
            index
            for index, device in enumerate(devices)
            if device["name"] == audio.rx_device
            and device["max_input_channels"] >= audio.rx_channels
        ]
        if len(matches) != 1:
            raise SpeechModelError("enrollment_rx_device_missing_or_ambiguous")
        print("仅录入配置中的 OOPZ RX，不使用系统默认麦克风。请让本次注册者独自讲话。")
        print("共 3 段，每段 8 秒；使用不同自然句子。音频仅留在内存，全部通过后保存声纹。")
        samples = []
        for index in range(3):
            input(f"第 {index + 1}/3 段：按 Enter 开始录入，或按 Ctrl-C 取消。")
            with sd.InputStream(
                device=matches[0],
                samplerate=SAMPLE_RATE,
                channels=audio.rx_channels,
                dtype="float32",
            ) as stream:
                captured, overflowed = stream.read(8 * SAMPLE_RATE)
            if overflowed:
                raise SpeechModelError("enrollment_audio_input_gap")
            if captured.shape != (8 * SAMPLE_RATE, audio.rx_channels):
                raise SpeechModelError("invalid_enrollment_capture")
            samples.append(_quality(captured.mean(axis=1, dtype=np.float32)))
            print(f"第 {index + 1}/3 段已结束。")
        return samples
    except SpeechModelError:
        raise
    except Exception:
        raise SpeechModelError("enrollment_capture_failed") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage private local speaker templates")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", default="oopz")
    commands = parser.add_subparsers(dest="command", required=True)
    enroll = commands.add_parser("enroll", help="Import clean WAVs or explicitly record OOPZ RX")
    enroll.add_argument("--speaker-id", required=True)
    enroll.add_argument("--name", required=True)
    source = enroll.add_mutually_exclusive_group(required=True)
    source.add_argument("--wav", type=Path, action="append")
    source.add_argument(
        "--record", action="store_true", help="Prompt before each of 3 RX recordings"
    )
    commands.add_parser("list")
    remove = commands.add_parser("remove")
    remove.add_argument("--speaker-id", required=True)
    args = parser.parse_args(argv)
    try:
        try:
            profile = load_settings(args.config).profiles[args.profile]
        except Exception:
            raise SpeechModelError("speaker_configuration_invalid") from None
        config = profile.speakers
        if config is None:
            raise SpeechModelError("speaker_configuration_required")
        if args.command == "enroll":
            if args.record:
                if profile.audio is None:
                    raise SpeechModelError("enrollment_rx_configuration_required")
                samples = record_enrollment(profile.audio)
            else:
                if not 3 <= len(args.wav) <= 16:
                    raise SpeechModelError("enrollment_requires_3_to_16_samples")
                samples = [read_enrollment_wav(path) for path in args.wav]
            hashes = {hashlib.sha256(pcm.tobytes()).digest() for pcm in samples}
            if len(hashes) != len(samples):
                raise SpeechModelError("enrollment_requires_distinct_recordings")
            model = CamPlus(config)
            registry = SpeakerRegistry(
                config.registry_path, model.fingerprint, model.dimension
            ).load()
            registry.enroll(args.speaker_id, args.name, [model.embedding(pcm) for pcm in samples])
            registry.save()
            print(
                json.dumps({"speaker_id": args.speaker_id, "name": args.name}, ensure_ascii=False)
            )
        else:
            registry = SpeakerRegistry.open_existing(config.registry_path, config.model_sha256)
            if args.command == "list":
                print(json.dumps(registry.list() if registry else [], ensure_ascii=False))
            else:
                if registry is None or not registry.remove(args.speaker_id):
                    raise SpeechModelError("speaker_not_registered")
                registry.save()
                print(json.dumps({"speaker_id": args.speaker_id}, ensure_ascii=False))
        return 0
    except SpeechModelError as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("enrollment_cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
