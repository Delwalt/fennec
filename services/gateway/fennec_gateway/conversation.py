from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
import json
import logging
import secrets
from time import monotonic
from typing import Any

from .audio import decode_audio_to_pcm16_mono
from .media import AssistantAudioTrack
from .providers import ConsumerProvider, FinalizedTurn, ProviderError, SpeechProvider
from .session_configuration import VoiceConfiguration
from .telemetry import SessionTelemetry
from .turns import SileroTurnDetector


logger = logging.getLogger("fennec.gateway")
EventSink = Callable[[str, dict[str, Any]], None]


class AudioBackpressureError(RuntimeError):
    """Raised when a client supplies audio faster than the bounded worker can process it."""


@dataclass(frozen=True, slots=True)
class FinalizedAudio:
    pcm: bytes
    forced_by_limit: bool
    speech_end_delay_ms: float


class ConversationSession:
    def __init__(
        self,
        *,
        session_id: str,
        speech: SpeechProvider,
        consumer: ConsumerProvider,
        output: AssistantAudioTrack,
        send_event: EventSink,
        detector: SileroTurnDetector | None = None,
        audio_queue_frames: int = 100,
        turn_queue_size: int = 4,
        phrase_delay_seconds: float = 0.4,
        max_continuation_characters: int = 8_192,
        configuration: VoiceConfiguration | None = None,
    ) -> None:
        self._session_id = session_id
        self._speech = speech
        self._consumer = consumer
        self._output = output
        self._send_event = send_event
        self._detector = detector or SileroTurnDetector()
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=audio_queue_frames)
        self._turn_queue: asyncio.Queue[FinalizedAudio] = asyncio.Queue(
            maxsize=turn_queue_size
        )
        self._phrase_delay_seconds = phrase_delay_seconds
        self._max_continuation_characters = max_continuation_characters
        self._configuration = configuration
        self._audio_worker = asyncio.create_task(self._run_audio_worker())
        self._turn_worker = asyncio.create_task(self._run_turn_worker())
        self._generation_task: asyncio.Task[None] | None = None
        self._generation_id: str | None = None
        self._assistant_active = False
        self._continuations_requested = 0
        self._continued_text: list[str] = []
        self._telemetry = SessionTelemetry()
        self._pending_interruption = False
        self._closed = False

    def feed_audio(self, pcm: bytes) -> None:
        if self._closed:
            return
        try:
            self._audio_queue.put_nowait(pcm)
            self._telemetry.observe_input_queue(self._audio_queue.qsize())
        except asyncio.QueueFull as error:
            self._emit("error", code="audio_backpressure", component="turn_detection")
            raise AudioBackpressureError("microphone audio queue reached its limit") from error

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._audio_worker.cancel()
        self._turn_worker.cancel()
        await asyncio.gather(
            self._audio_worker,
            self._turn_worker,
            return_exceptions=True,
        )
        await self._cancel_generation(reason="session_closed", notify=False)
        self._detector.reset()
        self._clear_audio_queue()
        self._clear_turn_queue()
        summary = self._telemetry.summary(
            output_queue_peak_frames=self._output.queued_peak_frames,
            stale_audio_frames_rejected=self._output.rejected_frames,
        )
        self._emit("telemetry.session.summary", **summary)
        logger.info(
            "telemetry.session.summary session_id=%s summary=%s",
            self._session_id,
            json.dumps(summary, separators=(",", ":")),
        )

    async def _run_audio_worker(self) -> None:
        try:
            while True:
                pcm = await self._audio_queue.get()
                detection = await asyncio.to_thread(self._detector.feed, pcm)
                if detection.speech_started:
                    confirmed_at = monotonic()
                    self._emit("speech.started")
                    if self._assistant_active:
                        self._pending_interruption = True
                        await self._cancel_generation(
                            reason="user_speech",
                            notify=True,
                            confirmed_at=confirmed_at,
                        )
                    elif (
                        self._generation_task is not None
                        and not self._generation_task.done()
                    ) or not self._turn_queue.empty():
                        self._continuations_requested += 1
                    self._emit("state.changed", state="listening")
                if detection.finalized_audio is not None:
                    try:
                        self._turn_queue.put_nowait(
                            FinalizedAudio(
                                pcm=detection.finalized_audio,
                                forced_by_limit=detection.forced_by_limit,
                                speech_end_delay_ms=detection.speech_end_delay_ms,
                            )
                        )
                    except asyncio.QueueFull as error:
                        self._emit(
                            "error",
                            code="turn_backpressure",
                            component="transcription",
                        )
                        raise AudioBackpressureError(
                            "finalized turn queue reached its limit"
                        ) from error
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audio worker failed session_id=%s", self._session_id)
            self._emit("error", code="turn_detection_failed", component="turn_detection")

    async def _run_turn_worker(self) -> None:
        try:
            while True:
                finalized = await self._turn_queue.get()
                task = asyncio.create_task(
                    self._run_turn(
                        finalized.pcm,
                        forced_by_limit=finalized.forced_by_limit,
                        speech_end_delay_ms=finalized.speech_end_delay_ms,
                    )
                )
                self._generation_task = task
                try:
                    await task
                except asyncio.CancelledError:
                    if self._closed:
                        raise
                finally:
                    if self._generation_task is task:
                        self._generation_task = None
        except asyncio.CancelledError:
            raise

    async def _run_turn(
        self,
        pcm: bytes,
        *,
        forced_by_limit: bool,
        speech_end_delay_ms: float,
    ) -> None:
        turn_id = secrets.token_urlsafe(12)
        generation_id = secrets.token_urlsafe(12)
        self._generation_id = generation_id
        speech_ended_at = monotonic() - speech_end_delay_ms / 1_000
        self._telemetry.turns_committed += 1
        self._telemetry.timing("endpoint_delay_ms", speech_end_delay_ms)
        if forced_by_limit:
            self._telemetry.forced_turns += 1
        self._emit(
            "turn.committed",
            turn_id=turn_id,
            generation_id=generation_id,
            forced_by_limit=forced_by_limit,
            audio_ms=round(len(pcm) / 32, 1),
            endpoint_delay_ms=round(speech_end_delay_ms, 1),
        )
        self._emit("state.changed", state="transcribing", turn_id=turn_id)

        try:
            if self._configuration is None:
                text = await self._speech.transcribe(pcm)
            else:
                text = await self._speech.transcribe(
                    pcm,
                    model=self._configuration.stt_model,
                    language=self._configuration.speech_language,
                )
            transcript_at = monotonic()
            if self._continuations_requested:
                self._continuations_requested -= 1
                self._append_continued_text(text)
                self._telemetry.continued_segments += 1
                self._emit("turn.deferred", turn_id=turn_id, reason="user_continuing")
                return

            combined_text = " ".join((*self._continued_text, text)).strip()
            self._continued_text.clear()
            if not combined_text:
                self._telemetry.empty_transcripts += 1
                if self._pending_interruption:
                    self._telemetry.possible_false_interruptions += 1
                self._pending_interruption = False
                self._emit("turn.ignored", turn_id=turn_id, reason="empty_transcript")
                self._emit("state.changed", state="listening")
                return

            transcript_latency_ms = (transcript_at - speech_ended_at) * 1_000
            self._telemetry.timing("transcript_final_ms", transcript_latency_ms)
            self._pending_interruption = False
            self._emit(
                "transcript.final",
                turn_id=turn_id,
                generation_id=generation_id,
                text=combined_text,
                latency_ms=round(transcript_latency_ms, 1),
            )
            self._emit("state.changed", state="waiting", generation_id=generation_id)
            self._assistant_active = True
            self._output.begin_generation(generation_id)
            first_audio = True
            turn = FinalizedTurn(
                session_id=self._session_id,
                turn_id=turn_id,
                generation_id=generation_id,
                text=combined_text,
            )
            async for phrase in response_phrases(
                self._consumer.respond(turn),
                max_delay_seconds=self._phrase_delay_seconds,
            ):
                if generation_id != self._generation_id:
                    return
                self._emit(
                    "assistant.text.delta",
                    generation_id=generation_id,
                    text=phrase,
                )
                if self._configuration is None:
                    wav = await self._speech.synthesize(phrase)
                else:
                    wav = await self._speech.synthesize(
                        phrase,
                        model=self._configuration.tts_model,
                        voice=self._configuration.tts_voice,
                    )
                pcm48 = await asyncio.to_thread(
                    decode_audio_to_pcm16_mono,
                    wav,
                    sample_rate=48_000,
                )
                queued = await self._output.enqueue_pcm(
                    generation_id=generation_id,
                    pcm=pcm48,
                )
                if not queued:
                    return
                if first_audio:
                    first_audio = False
                    first_audio_latency_ms = (monotonic() - speech_ended_at) * 1_000
                    self._telemetry.timing("first_audio_queued_ms", first_audio_latency_ms)
                    self._emit(
                        "assistant.speaking",
                        generation_id=generation_id,
                        latency_ms=round(first_audio_latency_ms, 1),
                    )
                    self._emit("state.changed", state="speaking", generation_id=generation_id)

            self._emit("assistant.done", generation_id=generation_id)
            self._telemetry.generations_completed += 1
            self._emit("state.changed", state="listening")
        except asyncio.CancelledError:
            self._output.cancel_generation(generation_id)
            raise
        except ProviderError as error:
            self._telemetry.provider_errors += 1
            self._output.cancel_generation(generation_id)
            logger.warning(
                "conversation provider failed session_id=%s generation_id=%s error=%s",
                self._session_id,
                generation_id,
                error,
            )
            self._emit(
                "error",
                code="conversation_provider_failed",
                component=_provider_component(error),
                generation_id=generation_id,
            )
            self._emit("state.changed", state="error")
        finally:
            self._assistant_active = False
            if self._generation_id == generation_id:
                self._generation_id = None

    async def _cancel_generation(
        self,
        *,
        reason: str,
        notify: bool,
        confirmed_at: float | None = None,
    ) -> None:
        generation_id = self._generation_id
        task = self._generation_task
        self._generation_id = None
        if generation_id is not None:
            self._output.cancel_generation(generation_id)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._generation_task = None
        if notify and generation_id is not None:
            latency_ms = 0.0
            if confirmed_at is not None:
                latency_ms = (monotonic() - confirmed_at) * 1_000
                self._telemetry.timing("interruption_cancel_ms", latency_ms)
            self._telemetry.interruptions += 1
            self._emit(
                "assistant.cancelled",
                generation_id=generation_id,
                reason=reason,
                latency_ms=round(latency_ms, 1),
            )

    def _emit(self, event_type: str, **data: Any) -> None:
        # Metadata only - never log spoken/spoken-back text content.
        redacted = {key: value for key, value in data.items() if key != "text"}
        logger.info(
            "event session_id=%s type=%s data=%s",
            self._session_id,
            event_type,
            json.dumps(redacted, separators=(",", ":")),
        )
        self._send_event(event_type, data)

    def _append_continued_text(self, text: str) -> None:
        if not text:
            return
        combined_characters = sum(len(part) for part in self._continued_text) + len(text)
        if combined_characters > self._max_continuation_characters:
            raise ProviderError("continued transcript exceeded the text limit")
        self._continued_text.append(text)

    def _clear_audio_queue(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def _clear_turn_queue(self) -> None:
        while True:
            try:
                self._turn_queue.get_nowait()
            except asyncio.QueueEmpty:
                return


class ConversationRuntime:
    def __init__(
        self,
        *,
        speech: SpeechProvider,
        consumer: ConsumerProvider,
        default_configuration: VoiceConfiguration | None = None,
        detector_factory: Callable[[VoiceConfiguration], SileroTurnDetector] | None = None,
    ) -> None:
        self.speech = speech
        self.consumer = consumer
        self._detector_factory = detector_factory
        self._default_configuration = default_configuration
        self._prepared_models: set[tuple[str, str]] = set()
        self._preparation_lock = asyncio.Lock()
        self._warm_task: asyncio.Task[None] | None = None
        self._ready = False
        self._error: str | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def status(self) -> str:
        if self._ready:
            return "ready"
        if self._error:
            return "failed"
        return "warming"

    def start(self) -> None:
        if self._warm_task is None:
            self._warm_task = asyncio.create_task(self._warm())

    def create_session(
        self,
        *,
        session_id: str,
        output: AssistantAudioTrack,
        send_event: EventSink,
        configuration: VoiceConfiguration | None = None,
    ) -> ConversationSession:
        if not self._ready:
            raise RuntimeError("conversation services are not ready")
        resolved = configuration or self._default_configuration
        if self._detector_factory is not None and resolved is not None:
            detector = self._detector_factory(resolved)
        elif resolved is not None:
            detector = SileroTurnDetector(
                threshold=resolved.vad_threshold,
                endpoint_silence_ms=resolved.endpoint_silence_ms,
                prefix_ms=resolved.prefix_ms,
                min_speech_ms=resolved.min_speech_ms,
                max_turn_seconds=resolved.max_turn_seconds,
            )
        else:
            detector = SileroTurnDetector()
        return ConversationSession(
            session_id=session_id,
            speech=self.speech,
            consumer=self.consumer,
            output=output,
            send_event=send_event,
            detector=detector,
            configuration=resolved,
        )

    async def prepare_configuration(self, configuration: VoiceConfiguration) -> None:
        models = (configuration.stt_model, configuration.tts_model)
        if models in self._prepared_models:
            return
        async with self._preparation_lock:
            if models in self._prepared_models:
                return
            await self.speech.ensure_models(
                stt_model=configuration.stt_model,
                tts_model=configuration.tts_model,
            )
            self._prepared_models.add(models)

    async def close(self) -> None:
        if self._warm_task is not None and not self._warm_task.done():
            self._warm_task.cancel()
            await asyncio.gather(self._warm_task, return_exceptions=True)
        await asyncio.gather(self.speech.close(), self.consumer.close(), return_exceptions=True)

    async def _warm(self) -> None:
        try:
            await self.consumer.health()
            if self._default_configuration is None:
                await self.speech.ensure_models()
            else:
                await self.prepare_configuration(self._default_configuration)
            await asyncio.to_thread(SileroTurnDetector.warm)
            if self._default_configuration is None:
                wav = await self.speech.synthesize("Fennec is ready.")
            else:
                wav = await self.speech.synthesize(
                    "Fennec is ready.",
                    model=self._default_configuration.tts_model,
                    voice=self._default_configuration.tts_voice,
                )
            pcm16 = await asyncio.to_thread(
                decode_audio_to_pcm16_mono,
                wav,
                sample_rate=16_000,
            )
            if self._default_configuration is None:
                transcript = await self.speech.transcribe(pcm16)
            else:
                transcript = await self.speech.transcribe(
                    pcm16,
                    model=self._default_configuration.stt_model,
                    language=self._default_configuration.speech_language,
                )
            if not transcript:
                raise ProviderError("local speech warm-up returned an empty transcript")
            self._ready = True
            logger.info("conversation services ready")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = type(error).__name__
            logger.exception("conversation services failed to warm")


async def response_phrases(
    deltas: AsyncIterator[str],
    *,
    max_delay_seconds: float,
    max_characters: int = 180,
) -> AsyncIterator[str]:
    iterator = deltas.__aiter__()
    pending: asyncio.Task[str] | None = asyncio.create_task(anext(iterator))
    buffer = ""
    try:
        while pending is not None:
            timeout = max_delay_seconds if buffer.strip() else None
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                phrase = buffer
                buffer = ""
                if phrase.strip():
                    yield phrase
                continue
            try:
                delta = pending.result()
            except StopAsyncIteration:
                pending = None
                break
            pending = asyncio.create_task(anext(iterator))
            buffer += delta
            while True:
                boundary = _phrase_boundary(buffer, max_characters=max_characters)
                if boundary is None:
                    break
                phrase = buffer[:boundary]
                buffer = buffer[boundary:]
                if phrase.strip():
                    yield phrase
        if buffer.strip():
            yield buffer
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def _phrase_boundary(text: str, *, max_characters: int) -> int | None:
    for index, character in enumerate(text):
        if character in ".?!;:\n" and index >= 7:
            boundary = index + 1
            while boundary < len(text) and text[boundary].isspace():
                boundary += 1
            return boundary
    if len(text) >= max_characters:
        split = text.rfind(" ", 0, max_characters)
        return split if split > 0 else max_characters
    return None


def _provider_component(error: ProviderError) -> str:
    message = str(error).lower()
    if "stt" in message:
        return "stt"
    if "tts" in message:
        return "tts"
    if "consumer" in message:
        return "consumer"
    return "speech"
