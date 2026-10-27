"""Local, pinned speech models and private speaker templates; never capture audio."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from .config import SpeakerConfig, TurnConfig

SAMPLE_RATE = 16000
MAX_REGISTRY_BYTES = 4 * 1024 * 1024


class SpeechModelError(ValueError):
    """A safe diagnostic without model paths, audio, or embedding contents."""


def _pcm(pcm: np.ndarray, max_seconds: int) -> np.ndarray:
    if (
        not isinstance(pcm, np.ndarray)
        or pcm.dtype != np.float32
        or pcm.ndim != 1
        or not 1 <= pcm.size <= max_seconds * SAMPLE_RATE
        or not np.isfinite(pcm).all()
        or np.max(np.abs(pcm)) > 1
    ):
        raise SpeechModelError("invalid_16khz_mono_float32_audio")
    return np.ascontiguousarray(pcm)


def _unit(vector: Any, dimension: int, *, stored: bool = False) -> np.ndarray:
    try:
        value = np.asarray(vector)
        if value.dtype.kind not in "fiu":
            raise ValueError
        value = value.astype(np.float64)
        if value.shape != (dimension,) or not np.isfinite(value).all():
            raise ValueError
        norm = float(np.linalg.norm(value))
        if not math.isfinite(norm) or norm < 1e-9 or (stored and abs(norm - 1) > 1e-4):
            raise ValueError
        return (value / norm).astype(np.float32)
    except (ValueError, TypeError, OverflowError):
        raise SpeechModelError("invalid_speaker_embedding") from None


def _checked_model(path: str, expected_sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SpeechModelError("invalid_model_fingerprint")
    try:
        source = Path(path)
        if not source.is_file() or not 1 <= source.stat().st_size <= 1024**3:
            raise SpeechModelError("model_unavailable")
        with source.open("rb") as model:
            actual = hashlib.file_digest(model, "sha256").hexdigest()
        if actual != expected_sha256:
            raise SpeechModelError("model_checksum_mismatch")
    except OSError:
        raise SpeechModelError("model_unavailable") from None
    return actual


class CamPlus:
    def __init__(self, config: SpeakerConfig):
        self.fingerprint = _checked_model(config.model_path, config.model_sha256)
        try:
            import sherpa_onnx

            options = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=config.model_path, num_threads=1, debug=False, provider="cpu"
            )
            if not options.validate():
                raise ValueError
            self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(options)
            self.dimension = int(self.extractor.dim)
            if not 1 <= self.dimension <= 4096:
                raise ValueError
        except Exception:
            raise SpeechModelError("camplus_initialization_failed") from None

    def embedding(self, pcm: np.ndarray) -> np.ndarray:
        samples = _pcm(pcm, 15)
        if samples.size < SAMPLE_RATE // 5:
            raise SpeechModelError("speaker_audio_too_short")
        try:
            stream = self.extractor.create_stream()
            stream.accept_waveform(sample_rate=SAMPLE_RATE, waveform=samples)
            stream.input_finished()
            if not self.extractor.is_ready(stream):
                raise ValueError
            return _unit(self.extractor.compute(stream), self.dimension)
        except Exception:
            raise SpeechModelError("camplus_inference_failed") from None


class SmartTurn:
    def __init__(self, config: TurnConfig):
        self.fingerprint = _checked_model(config.model_path, config.model_sha256)
        try:
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor

            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = 1
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            options.log_severity_level = 3
            self.session = ort.InferenceSession(
                config.model_path, sess_options=options, providers=["CPUExecutionProvider"]
            )
            inputs = self.session.get_inputs()
            if (
                len(inputs) != 1
                or inputs[0].name != "input_features"
                or inputs[0].type != "tensor(float)"
                or len(inputs[0].shape) != 3
                or inputs[0].shape[1:] != [80, 800]
                or not (inputs[0].shape[0] == 1 or isinstance(inputs[0].shape[0], str))
            ):
                raise ValueError
            self.extractor = WhisperFeatureExtractor(chunk_length=8)
        except Exception:
            raise SpeechModelError("smart_turn_initialization_failed") from None

    def probability(self, pcm: np.ndarray) -> float:
        samples = _pcm(pcm, 60)[-8 * SAMPLE_RATE :]
        samples = np.pad(samples, (8 * SAMPLE_RATE - samples.size, 0))
        try:
            features = self.extractor(
                samples,
                sampling_rate=SAMPLE_RATE,
                return_tensors="np",
                padding="max_length",
                max_length=8 * SAMPLE_RATE,
                truncation=True,
                do_normalize=True,
            ).input_features
            features = np.asarray(features, dtype=np.float32)
            if features.shape != (1, 80, 800) or not np.isfinite(features).all():
                raise ValueError
            outputs = self.session.run(None, {"input_features": features})
            probability = np.asarray(outputs[0])
            if probability.size != 1:
                raise ValueError
            result = float(probability.item())
            if not math.isfinite(result) or not 0 <= result <= 1:
                raise ValueError
            return result
        except Exception:
            raise SpeechModelError("smart_turn_inference_failed") from None


@dataclass(frozen=True)
class SpeakerMatch:
    speaker_id: str
    name: str
    score: float
    margin: float


def _identity(speaker_id: Any, name: Any) -> None:
    if (
        not isinstance(speaker_id, str)
        or not re.fullmatch(r"[\w.:-]{1,128}", speaker_id)
        or not isinstance(name, str)
        or not 1 <= len(name) <= 128
        or not name.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise SpeechModelError("invalid_speaker_identity")


def _private(path: Path, directory: bool = False) -> None:
    info = path.lstat()
    if (
        not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
        or info.st_mode & 0o077
        or (hasattr(os, "getuid") and info.st_uid != os.getuid())
    ):
        raise SpeechModelError("speaker_registry_requires_private_storage")


class SpeakerRegistry:
    def __init__(self, path: str | Path, model_fingerprint: str, dimension: int):
        if not re.fullmatch(r"[0-9a-f]{64}", model_fingerprint):
            raise SpeechModelError("invalid_model_fingerprint")
        if type(dimension) is not int or not 1 <= dimension <= 4096:
            raise SpeechModelError("invalid_embedding_dimension")
        self.path = Path(path)
        self.model_fingerprint = model_fingerprint
        self.dimension = dimension
        self._speakers: dict[str, tuple[str, np.ndarray]] = {}
        self._loaded_digest: str | None = None

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            if not path.parent.exists() and not path.parent.is_symlink():
                return None
            _private(path.parent, directory=True)
            if not path.exists() and not path.is_symlink():
                return None
            _private(path)
            with path.open("rb") as source:
                raw = source.read(MAX_REGISTRY_BYTES + 1)
            if len(raw) > MAX_REGISTRY_BYTES:
                raise SpeechModelError("speaker_registry_too_large")
            return raw
        except OSError:
            raise SpeechModelError("speaker_registry_unavailable") from None

    @classmethod
    def open_existing(cls, path: str | Path, fingerprint: str) -> SpeakerRegistry | None:
        """Inspect/list/remove without loading any inference runtime or model."""
        raw = cls._read(Path(path))
        if raw is None:
            return None
        try:
            dimension = json.loads(raw)["dimension"]
        except (ValueError, KeyError, TypeError):
            raise SpeechModelError("invalid_speaker_registry") from None
        return cls(path, fingerprint, dimension).load()

    def load(self) -> SpeakerRegistry:
        raw = self._read(self.path)
        self._speakers = {}
        self._loaded_digest = None
        if raw is None:
            return self
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != {
                "version",
                "model_fingerprint",
                "dimension",
                "speakers",
            }:
                raise ValueError
            if data["version"] != 1 or type(data["version"]) is not int:
                raise ValueError
            if data["model_fingerprint"] != self.model_fingerprint:
                raise SpeechModelError("speaker_model_changed_reenrollment_required")
            if data["dimension"] != self.dimension or type(data["dimension"]) is not int:
                raise ValueError
            records = data["speakers"]
            if not isinstance(records, list) or len(records) > 128:
                raise ValueError
            speakers: dict[str, tuple[str, np.ndarray]] = {}
            for record in records:
                if not isinstance(record, dict) or set(record) != {"speaker_id", "name", "vector"}:
                    raise ValueError
                speaker_id, name = record["speaker_id"], record["name"]
                _identity(speaker_id, name)
                if speaker_id in speakers:
                    raise ValueError
                speakers[speaker_id] = (name, _unit(record["vector"], self.dimension, stored=True))
            self._speakers = speakers
            self._loaded_digest = hashlib.sha256(raw).hexdigest()
        except SpeechModelError:
            raise
        except (ValueError, KeyError, TypeError):
            raise SpeechModelError("invalid_speaker_registry") from None
        return self

    def list(self) -> list[dict[str, str]]:
        return [{"speaker_id": key, "name": value[0]} for key, value in self._speakers.items()]

    def enroll(
        self, speaker_id: str, name: str, embeddings: list[np.ndarray], *, replace: bool = False
    ) -> None:
        _identity(speaker_id, name)
        if speaker_id in self._speakers and not replace:
            raise SpeechModelError("speaker_already_registered")
        if speaker_id not in self._speakers and len(self._speakers) >= 128:
            raise SpeechModelError("speaker_registry_full")
        if not 3 <= len(embeddings) <= 16:
            raise SpeechModelError("enrollment_requires_3_to_16_samples")
        vectors = np.stack([_unit(value, self.dimension) for value in embeddings])
        similarities = vectors @ vectors.T
        if float(np.min(similarities)) < 0.65:
            raise SpeechModelError("enrollment_samples_inconsistent")
        self._speakers[speaker_id] = (name, _unit(vectors.mean(axis=0), self.dimension))

    def remove(self, speaker_id: str) -> bool:
        return self._speakers.pop(speaker_id, None) is not None

    def identify(
        self, embedding: np.ndarray, threshold: float, margin: float
    ) -> SpeakerMatch | None:
        if not math.isfinite(threshold) or not 0 < threshold <= 1:
            raise SpeechModelError("invalid_speaker_threshold")
        if not math.isfinite(margin) or not 0 <= margin <= 2:
            raise SpeechModelError("invalid_speaker_margin")
        vector = _unit(embedding, self.dimension)
        scored = sorted(
            (
                (float(np.dot(vector, item[1])), key, item[0])
                for key, item in self._speakers.items()
            ),
            reverse=True,
        )
        if not scored:
            return None
        score, speaker_id, name = scored[0]
        gap = score - scored[1][0] if len(scored) > 1 else score + 1
        if score < threshold or gap < margin:
            return None
        return SpeakerMatch(speaker_id, name, score, gap)

    def save(self) -> None:
        raw = json.dumps(
            {
                "version": 1,
                "model_fingerprint": self.model_fingerprint,
                "dimension": self.dimension,
                "speakers": [
                    {"speaker_id": key, "name": item[0], "vector": item[1].tolist()}
                    for key, item in self._speakers.items()
                ],
            },
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(raw) > MAX_REGISTRY_BYTES:
            raise SpeechModelError("speaker_registry_too_large")
        lock_path = self.path.with_name(self.path.name + ".lock")
        temporary: str | None = None
        lock_fd: int | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _private(self.path.parent, directory=True)
            # An exclusive lock and digest prevent a second CLI overwriting a newer registry.
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            current = self._read(self.path)
            digest = hashlib.sha256(current).hexdigest() if current is not None else None
            if digest != self._loaded_digest:
                raise SpeechModelError("speaker_registry_changed_reload_required")
            fd, temporary = tempfile.mkstemp(prefix=".speakers-", dir=self.path.parent)
            with os.fdopen(fd, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self._loaded_digest = hashlib.sha256(raw).hexdigest()
        except FileExistsError:
            raise SpeechModelError("speaker_registry_busy") from None
        except OSError:
            raise SpeechModelError("speaker_registry_write_failed") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
            if lock_fd is not None:
                os.close(lock_fd)
                lock_path.unlink(missing_ok=True)
