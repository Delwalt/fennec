from __future__ import annotations

from array import array
import asyncio
from dataclasses import dataclass
from fractions import Fraction
import math

from aiortc import MediaStreamTrack
from av import AudioFrame


@dataclass(frozen=True, slots=True)
class PlaybackFrame:
    generation_id: str
    pcm: bytes


class AssistantAudioTrack(MediaStreamTrack):
    """A paced, bounded assistant track with generation-aware stale-audio rejection."""

    kind = "audio"
    sample_rate = 48_000
    samples_per_frame = 960

    def __init__(self, *, queue_frames: int = 500) -> None:
        super().__init__()
        self._pts = 0
        self._started_at: float | None = None
        self._tone_until = 0.0
        self._active_generation: str | None = None
        self._queue: asyncio.Queue[PlaybackFrame] = asyncio.Queue(maxsize=queue_frames)
        self._queued_peak = 0
        self._rejected_frames = 0

    def trigger(self, *, duration_seconds: float = 0.45) -> None:
        loop = asyncio.get_running_loop()
        self._tone_until = max(self._tone_until, loop.time() + duration_seconds)

    @property
    def queued_frames(self) -> int:
        return self._queue.qsize()

    @property
    def queued_peak_frames(self) -> int:
        return self._queued_peak

    @property
    def rejected_frames(self) -> int:
        return self._rejected_frames

    def begin_generation(self, generation_id: str) -> None:
        self._active_generation = generation_id
        self._clear_queue()

    def cancel_generation(self, generation_id: str) -> None:
        if generation_id == self._active_generation:
            self._active_generation = None
            self._clear_queue()

    async def enqueue_pcm(self, *, generation_id: str, pcm: bytes) -> bool:
        if generation_id != self._active_generation:
            return False
        frame_bytes = self.samples_per_frame * 2
        for offset in range(0, len(pcm), frame_bytes):
            if generation_id != self._active_generation:
                return False
            chunk = pcm[offset : offset + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk = chunk + bytes(frame_bytes - len(chunk))
            await self._queue.put(PlaybackFrame(generation_id=generation_id, pcm=chunk))
            self._queued_peak = max(self._queued_peak, self._queue.qsize())
        return True

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._rejected_frames += 1
            except asyncio.QueueEmpty:
                return

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        if self._started_at is None:
            self._started_at = loop.time()
        target = self._started_at + self._pts / self.sample_rate
        delay = target - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        pcm = self._next_pcm(loop.time())

        frame = AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(pcm)
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.sample_rate)
        self._pts += self.samples_per_frame
        return frame

    def _next_pcm(self, now: float) -> bytes:
        if now < self._tone_until:
            samples = array("h")
            for index in range(self.samples_per_frame):
                absolute_sample = self._pts + index
                samples.append(
                    int(7_000 * math.sin(2 * math.pi * 440 * absolute_sample / self.sample_rate))
                )
            return samples.tobytes()

        while True:
            try:
                playback = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return bytes(self.samples_per_frame * 2)
            if playback.generation_id == self._active_generation:
                return playback.pcm
            self._rejected_frames += 1
