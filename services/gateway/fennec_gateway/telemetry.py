from __future__ import annotations

from collections import deque
from math import ceil
from statistics import median
from time import monotonic


class SessionTelemetry:
    """Bounded, text-free measurements for one conversation session."""

    def __init__(self, *, sample_limit: int = 512) -> None:
        self._started_at = monotonic()
        self._samples: dict[str, deque[float]] = {
            "endpoint_delay_ms": deque(maxlen=sample_limit),
            "transcript_final_ms": deque(maxlen=sample_limit),
            "first_audio_queued_ms": deque(maxlen=sample_limit),
            "interruption_cancel_ms": deque(maxlen=sample_limit),
        }
        self.turns_committed = 0
        self.generations_completed = 0
        self.interruptions = 0
        self.continued_segments = 0
        self.possible_false_interruptions = 0
        self.empty_transcripts = 0
        self.forced_turns = 0
        self.provider_errors = 0
        self.input_queue_peak_frames = 0

    def timing(self, name: str, milliseconds: float) -> None:
        self._samples[name].append(max(0.0, milliseconds))

    def observe_input_queue(self, frames: int) -> None:
        self.input_queue_peak_frames = max(self.input_queue_peak_frames, frames)

    def summary(
        self,
        *,
        output_queue_peak_frames: int,
        stale_audio_frames_rejected: int,
    ) -> dict[str, object]:
        return {
            "duration_ms": round((monotonic() - self._started_at) * 1_000, 1),
            "turns_committed": self.turns_committed,
            "generations_completed": self.generations_completed,
            "interruptions": self.interruptions,
            "continued_segments": self.continued_segments,
            "possible_false_interruptions": self.possible_false_interruptions,
            "empty_transcripts": self.empty_transcripts,
            "forced_turns": self.forced_turns,
            "provider_errors": self.provider_errors,
            "input_queue_peak_frames": self.input_queue_peak_frames,
            "output_queue_peak_frames": output_queue_peak_frames,
            "stale_audio_frames_rejected": stale_audio_frames_rejected,
            "latency_ms": {
                name: _distribution(samples)
                for name, samples in self._samples.items()
            },
        }


def _distribution(samples: deque[float]) -> dict[str, float | int | None]:
    if not samples:
        return {"count": 0, "median": None, "p95": None, "max": None}
    ordered = sorted(samples)
    return {
        "count": len(ordered),
        "median": round(median(ordered), 1),
        "p95": round(ordered[max(0, ceil(len(ordered) * 0.95) - 1)], 1),
        "max": round(ordered[-1], 1),
    }
