from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from faster_whisper.vad import VadOptions, get_speech_timestamps, get_vad_model
import numpy as np


TimestampDetector = Callable[[np.ndarray, VadOptions], list[dict[str, int]]]


@dataclass(frozen=True, slots=True)
class TurnDetection:
    speech_started: bool = False
    finalized_audio: bytes | None = None
    forced_by_limit: bool = False
    speech_end_delay_ms: float = 0.0


class SileroTurnDetector:
    sample_rate = 16_000
    sample_width = 2

    def __init__(
        self,
        *,
        threshold: float = 0.5,
        endpoint_silence_ms: int = 1_200,
        prefix_ms: int = 320,
        min_speech_ms: int = 160,
        max_turn_seconds: int = 30,
        evaluate_every_ms: int = 96,
        timestamp_detector: TimestampDetector | None = None,
    ) -> None:
        self._options = VadOptions(
            threshold=threshold,
            min_speech_duration_ms=min_speech_ms,
            max_speech_duration_s=max_turn_seconds,
            min_silence_duration_ms=endpoint_silence_ms,
            speech_pad_ms=0,
        )
        self._prefix_bytes = self._bytes_for_ms(prefix_ms)
        self._endpoint_samples = self.sample_rate * endpoint_silence_ms // 1_000
        self._max_bytes = self.sample_rate * self.sample_width * max_turn_seconds
        self._evaluate_bytes = self._bytes_for_ms(evaluate_every_ms)
        self._timestamp_detector = timestamp_detector or self._detect_timestamps
        self._pending = bytearray()
        self._utterance = bytearray()
        self._active = False
        self._bytes_since_evaluation = 0

    @staticmethod
    def warm() -> None:
        get_vad_model()(np.zeros(512, dtype=np.float32))

    def feed(self, pcm: bytes) -> TurnDetection:
        if len(pcm) % self.sample_width:
            raise ValueError("PCM input must contain complete 16-bit samples")

        target = self._utterance if self._active else self._pending
        target.extend(pcm)
        if not self._active and len(self._pending) > self._prefix_bytes:
            del self._pending[: len(self._pending) - self._prefix_bytes]

        self._bytes_since_evaluation += len(pcm)
        forced = self._active and len(self._utterance) >= self._max_bytes
        if not forced and self._bytes_since_evaluation < self._evaluate_bytes:
            return TurnDetection()
        self._bytes_since_evaluation = 0

        if not self._active:
            speeches = self._timestamps(self._pending)
            if not speeches:
                return TurnDetection()
            self._active = True
            self._utterance = self._pending
            self._pending = bytearray()
            return TurnDetection(speech_started=True)

        if forced:
            return self._finalize(forced=True)

        speeches = self._timestamps(self._utterance)
        if not speeches:
            return TurnDetection()
        samples = len(self._utterance) // self.sample_width
        silence_samples = samples - speeches[-1]["end"]
        if silence_samples >= self._endpoint_samples:
            trailing_samples = min(self.sample_rate // 5, silence_samples)
            end_sample = speeches[-1]["end"] + trailing_samples
            end_byte = end_sample * self.sample_width
            return self._finalize(
                audio=bytes(self._utterance[:end_byte]),
                speech_end_delay_ms=silence_samples * 1_000 / self.sample_rate,
            )
        return TurnDetection()

    def reset(self) -> None:
        self._pending.clear()
        self._utterance.clear()
        self._active = False
        self._bytes_since_evaluation = 0

    def _timestamps(self, pcm: bytearray) -> list[dict[str, int]]:
        if len(pcm) < 1_024:
            return []
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32_768.0
        return self._timestamp_detector(audio, self._options)

    @staticmethod
    def _detect_timestamps(audio: np.ndarray, options: VadOptions) -> list[dict[str, int]]:
        return get_speech_timestamps(audio, options)

    def _finalize(
        self,
        *,
        audio: bytes | None = None,
        forced: bool = False,
        speech_end_delay_ms: float = 0.0,
    ) -> TurnDetection:
        finalized = audio if audio is not None else bytes(self._utterance[: self._max_bytes])
        self.reset()
        return TurnDetection(
            finalized_audio=finalized,
            forced_by_limit=forced,
            speech_end_delay_ms=speech_end_delay_ms,
        )

    def _bytes_for_ms(self, milliseconds: int) -> int:
        return self.sample_rate * self.sample_width * milliseconds // 1_000
