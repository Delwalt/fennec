from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher
import json
import logging
import re
import secrets
from time import monotonic
from typing import Any

from .audio import decode_audio_to_pcm16_mono, power_dbfs, rms_power
from .config import DEFAULT_TENANT_ID
from .media import AssistantAudioTrack
from .providers import ConsumerProvider, FinalizedTurn, ProviderError, SpeechProvider
from .session_configuration import VoiceConfiguration
from .telemetry import SessionTelemetry
from .turns import SileroTurnDetector, TurnDetection


logger = logging.getLogger("fennec.gateway")
EventSink = Callable[[str, dict[str, Any]], None]
WARM_RETRY_SECONDS = 5
MAX_WARM_RETRY_SECONDS = 60
# ponytail: fixed allowance for the browser's jitter buffer. A real playout
# estimate would need RTCP receiver reports read back from the peer connection.
PLAYBACK_TAIL_SECONDS = 0.25
BARGE_IN_MARGIN_DB = 10.0
INITIAL_NOISE_FLOOR_DBFS = -60.0
NOISE_FLOOR_RISE_ALPHA = 0.01
NOISE_FLOOR_FALL_ALPHA = 0.1
NOISE_FLOOR_MAX_RISE_DB = 6.0
ECHO_TEXT_CHARACTERS = 8_192
MIN_ECHO_TOKENS = 4
ECHO_TOKEN_COVERAGE = 0.8
TOKEN_PATTERN = re.compile(r"[\w']+", re.UNICODE)
# Wordless sounds only. "yes", "no", "ok" and "stop" are answers however short they
# are, and dropping one because it was brief is worse than answering a hum.
BACKCHANNEL_SOUNDS = frozenset(
    {"hm", "hmm", "hmmm", "mm", "mmm", "mhm", "mmhm", "mmhmm", "uhhuh", "huh",
     "uh", "um", "erm", "er", "ah", "aha", "oh"}
)


class AudioBackpressureError(RuntimeError):
    """Raised when a client supplies audio faster than the bounded worker can process it."""


@dataclass(frozen=True, slots=True)
class FinalizedAudio:
    pcm: bytes
    forced_by_limit: bool
    speech_end_delay_ms: float
    detected_at: float
    confirmed: bool = True
    echo_generation_id: str | None = None
    cancelled_generation_id: str | None = None


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
        if turn_queue_size < 1:
            raise ValueError("turn_queue_size must be at least one")
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
        self._echo_generation_id: str | None = None
        self._echo_risk_until: float | None = None
        self._echo_text: dict[str, str] = {}
        self._echo_context_limit = max(8, turn_queue_size + 2)
        self._noise_floor_power = 10 ** (INITIAL_NOISE_FLOOR_DBFS / 10)
        self._candidate_started_at: float | None = None
        self._candidate_confirmed = False
        self._candidate_echo_generation_id: str | None = None
        self._candidate_cancelled_generation_id: str | None = None
        self._continuations_requested = 0
        self._continued_text: list[str] = []
        self._telemetry = SessionTelemetry()
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
        self._reset_candidate_state()
        self._echo_text.clear()
        self._echo_generation_id = None
        self._echo_risk_until = None
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
                fed_at = monotonic()
                detection = await asyncio.to_thread(self._detector.feed, pcm)
                observed_at = monotonic()
                self._telemetry.timing("vad_feed_ms", (observed_at - fed_at) * 1_000)
                self._observe_noise_floor(
                    pcm,
                    candidate_active=detection.candidate_active or detection.speech_started,
                )
                await self._apply_candidate_evidence(detection, observed_at=observed_at)
                if detection.finalized_audio is not None:
                    tracked_candidate = self._candidate_started_at is not None
                    finalized = FinalizedAudio(
                        pcm=detection.finalized_audio,
                        forced_by_limit=detection.forced_by_limit,
                        speech_end_delay_ms=detection.speech_end_delay_ms,
                        detected_at=monotonic(),
                        # Scripted adapters and older detector fakes can finalize a complete
                        # turn without first reporting its candidate transition.
                        confirmed=self._candidate_confirmed or not tracked_candidate,
                        echo_generation_id=self._candidate_echo_generation_id,
                        cancelled_generation_id=self._candidate_cancelled_generation_id,
                    )
                    self._reset_candidate_state()
                    self._enqueue_finalized(finalized)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("audio worker failed session_id=%s", self._session_id)
            self._emit("error", code="turn_detection_failed", component="turn_detection")

    async def _apply_candidate_evidence(
        self,
        detection: TurnDetection,
        *,
        observed_at: float,
    ) -> None:
        if (
            detection.finalized_audio is None
            and not detection.candidate_active
            and not detection.speech_started
        ):
            self._abandon_candidate()
            return

        if detection.speech_started and self._candidate_started_at is None:
            self._candidate_started_at = observed_at - detection.speech_duration_ms / 1_000
            self._candidate_echo_generation_id = self._active_echo_generation(observed_at)
            if self._candidate_echo_generation_id is not None:
                self._telemetry.echo_candidates_deferred += 1

        if self._candidate_started_at is None or self._candidate_confirmed:
            return
        if self._candidate_echo_generation_id is None:
            await self._confirm_candidate(detection, confirmed_at=observed_at)
            return
        if not detection.candidate_evaluated:
            return

        level_dbfs = max(
            detection.recent_speech_level_dbfs,
            detection.speech_level_dbfs,
        )
        noise_floor_dbfs = power_dbfs(self._noise_floor_power)
        margin_db = level_dbfs - noise_floor_dbfs
        self._telemetry.measurement("echo_candidate_level_dbfs", level_dbfs)
        self._telemetry.measurement("echo_candidate_margin_db", margin_db)
        if margin_db >= BARGE_IN_MARGIN_DB:
            self._telemetry.echo_candidates_confirmed += 1
            await self._confirm_candidate(detection, confirmed_at=observed_at)

    async def _confirm_candidate(
        self,
        detection: TurnDetection,
        *,
        confirmed_at: float,
    ) -> None:
        if self._candidate_confirmed:
            return
        self._candidate_confirmed = True
        confirmation_ms = 0.0
        if self._candidate_started_at is not None:
            confirmation_ms = (confirmed_at - self._candidate_started_at) * 1_000
            self._telemetry.timing("candidate_confirmation_ms", confirmation_ms)
        level_dbfs = max(
            detection.recent_speech_level_dbfs,
            detection.speech_level_dbfs,
        )
        noise_floor_dbfs = power_dbfs(self._noise_floor_power)
        self._emit(
            "speech.started",
            level_dbfs=level_dbfs,
            noise_floor_dbfs=noise_floor_dbfs,
            confirmation_ms=round(confirmation_ms, 1),
        )
        if self._assistant_active:
            # Purge before awaiting the cancellation: that await lets the turn
            # worker resume and pull the stale echo turn out of the queue.
            if self._generation_id is not None:
                self._drop_unconfirmed_for_generation(self._generation_id)
            self._candidate_cancelled_generation_id = await self._cancel_generation(
                reason="user_speech",
                notify=True,
                confirmed_at=confirmed_at,
                level_dbfs=level_dbfs,
            )
        elif (
            self._generation_task is not None
            and not self._generation_task.done()
        ) or not self._turn_queue.empty():
            self._continuations_requested += 1
        self._emit("state.changed", state="listening")

    def _observe_noise_floor(self, pcm: bytes, *, candidate_active: bool) -> None:
        if candidate_active or self._active_echo_generation() is not None:
            return
        observed = rms_power(pcm)
        max_rise = self._noise_floor_power * 10 ** (NOISE_FLOOR_MAX_RISE_DB / 10)
        bounded = min(observed, max_rise)
        alpha = (
            NOISE_FLOOR_FALL_ALPHA
            if bounded < self._noise_floor_power
            else NOISE_FLOOR_RISE_ALPHA
        )
        initial_floor = 10 ** (INITIAL_NOISE_FLOOR_DBFS / 10)
        self._noise_floor_power = max(
            initial_floor,
            self._noise_floor_power + alpha * (bounded - self._noise_floor_power),
        )

    def _enqueue_finalized(self, finalized: FinalizedAudio) -> None:
        try:
            self._turn_queue.put_nowait(finalized)
        except asyncio.QueueFull as error:
            if not finalized.confirmed:
                self._telemetry.dropped_unconfirmed_capacity += 1
                return
            self._emit(
                "error",
                code="turn_backpressure",
                component="transcription",
            )
            raise AudioBackpressureError(
                "finalized turn queue reached its limit"
            ) from error

    def _drop_unconfirmed_for_generation(self, generation_id: str) -> None:
        retained: list[FinalizedAudio] = []
        dropped = 0
        while True:
            try:
                candidate = self._turn_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not candidate.confirmed and candidate.echo_generation_id == generation_id:
                dropped += 1
            else:
                retained.append(candidate)
        for candidate in retained:
            self._turn_queue.put_nowait(candidate)
        self._telemetry.dropped_unconfirmed_generation_cancelled += dropped

    def _abandon_candidate(self) -> None:
        cancelled = self._candidate_cancelled_generation_id
        if cancelled is not None:
            # A reply was stopped for speech that turned out to be nothing at all.
            # Fennec cannot resume the audio it binned, so it says what happened and
            # leaves carrying on from there to the consumer.
            self._telemetry.abandoned_after_cancellation += 1
            self._telemetry.possible_false_interruptions += 1
            self._emit("speech.abandoned", generation_id=cancelled)
        self._reset_candidate_state()

    def _reset_candidate_state(self) -> None:
        self._candidate_started_at = None
        self._candidate_confirmed = False
        self._candidate_echo_generation_id = None
        self._candidate_cancelled_generation_id = None

    async def _run_turn_worker(self) -> None:
        try:
            while True:
                finalized = await self._turn_queue.get()
                task = asyncio.create_task(self._run_turn(finalized))
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

    async def _run_turn(self, finalized: FinalizedAudio) -> None:
        pcm = finalized.pcm
        speech_end_delay_ms = finalized.speech_end_delay_ms
        turn_id = secrets.token_urlsafe(12)
        generation_id = secrets.token_urlsafe(12)
        self._generation_id = generation_id
        started_at = monotonic()
        speech_ended_at = started_at - speech_end_delay_ms / 1_000
        queue_ms = (started_at - finalized.detected_at) * 1_000
        self._telemetry.turns_committed += 1
        self._telemetry.timing("turn_queue_ms", queue_ms)
        if finalized.forced_by_limit:
            self._telemetry.forced_turns += 1
        self._emit(
            "turn.committed",
            turn_id=turn_id,
            generation_id=generation_id,
            forced_by_limit=finalized.forced_by_limit,
            audio_ms=round(len(pcm) / 32, 1),
            endpoint_delay_ms=round(speech_end_delay_ms, 1),
        )
        self._emit("state.changed", state="transcribing", turn_id=turn_id)

        try:
            stt_started_at = monotonic()
            if self._configuration is None:
                text = await self._speech.transcribe(pcm)
            else:
                text = await self._speech.transcribe(
                    pcm,
                    model=self._configuration.stt_model,
                    language=self._configuration.speech_language,
                )
            transcript_at = monotonic()
            if finalized.echo_generation_id is not None:
                assistant_text = self._echo_text.get(finalized.echo_generation_id)
                ignored: str | None = None
                if _is_backchannel(text):
                    self._telemetry.backchannel_turns += 1
                    ignored = "backchannel"
                elif assistant_text is None:
                    self._telemetry.echo_reference_misses += 1
                elif _is_assistant_echo(text, assistant_text):
                    self._telemetry.assistant_echo_turns += 1
                    ignored = "assistant_echo"
                if ignored is not None:
                    if finalized.cancelled_generation_id is not None:
                        self._telemetry.possible_false_interruptions += 1
                    self._emit("turn.ignored", turn_id=turn_id, reason=ignored)
                    self._emit("state.changed", state="listening")
                    return
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
                if not finalized.confirmed:
                    self._telemetry.empty_unconfirmed_candidates += 1
                if finalized.cancelled_generation_id is not None:
                    self._telemetry.possible_false_interruptions += 1
                self._emit("turn.ignored", turn_id=turn_id, reason="empty_transcript")
                self._emit("state.changed", state="listening")
                return

            transcript_latency_ms = (transcript_at - speech_ended_at) * 1_000
            self._telemetry.timing("transcript_final_ms", transcript_latency_ms)
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
            first_delta_at: list[float] = []
            respond_at = monotonic()
            turn = FinalizedTurn(
                session_id=self._session_id,
                turn_id=turn_id,
                generation_id=generation_id,
                text=combined_text,
            )
            async for phrase in response_phrases(
                _stamp_first_delta(self._consumer.respond(turn), first_delta_at),
                max_delay_seconds=self._phrase_delay_seconds,
            ):
                phrase_at = monotonic()
                if generation_id != self._generation_id:
                    return
                self._emit(
                    "assistant.text.delta",
                    generation_id=generation_id,
                    text=phrase,
                )
                self._remember_assistant_text(generation_id, phrase)
                if self._configuration is None:
                    wav = await self._speech.synthesize(phrase)
                else:
                    wav = await self._speech.synthesize(
                        phrase,
                        model=self._configuration.tts_model,
                        voice=self._configuration.tts_voice,
                    )
                synthesized_at = monotonic()
                pcm48 = await asyncio.to_thread(
                    decode_audio_to_pcm16_mono,
                    wav,
                    sample_rate=48_000,
                )
                decoded_at = monotonic()
                queued = await self._output.enqueue_pcm(
                    generation_id=generation_id,
                    pcm=pcm48,
                )
                if not queued:
                    return
                if first_audio:
                    first_audio = False
                    queued_at = monotonic()
                    self._begin_echo_risk(generation_id)
                    self._report_first_audio(
                        turn_id=turn_id,
                        generation_id=generation_id,
                        stages={
                            "endpoint_delay_ms": speech_end_delay_ms,
                            "stt_ms": (transcript_at - stt_started_at) * 1_000,
                            "llm_first_delta_ms": (
                                (first_delta_at[0] if first_delta_at else phrase_at) - respond_at
                            )
                            * 1_000,
                            "phrase_ms": (
                                phrase_at - (first_delta_at[0] if first_delta_at else respond_at)
                            )
                            * 1_000,
                            "tts_ms": (synthesized_at - phrase_at) * 1_000,
                            "decode_ms": (decoded_at - synthesized_at) * 1_000,
                            "enqueue_ms": (queued_at - decoded_at) * 1_000,
                            "first_audio_queued_ms": (queued_at - speech_ended_at) * 1_000,
                        },
                        # Against tts_ms this is the synthesizer's real-time
                        # factor: above 1.0 means TTS cannot keep the speaker fed.
                        speech_ms=len(pcm48) / 96,
                        turn_queue_ms=queue_ms,
                    )
                    self._emit("state.changed", state="speaking", generation_id=generation_id)

            self._telemetry.timing("llm_total_ms", (monotonic() - respond_at) * 1_000)
            # The last phrase is enqueued seconds before the speaker finishes
            # saying it. Announcing "listening" here dropped the session back to
            # idle mid-sentence, so Fennec's own tail arrived as a fresh user
            # turn instead of a barge-in against a still-speaking assistant.
            await asyncio.sleep(self._output.queued_seconds + PLAYBACK_TAIL_SECONDS)
            self._clear_echo_risk(generation_id)
            self._emit("assistant.done", generation_id=generation_id)
            self._telemetry.generations_completed += 1
            self._emit("state.changed", state="listening")
        except asyncio.CancelledError:
            self._output.cancel_generation(generation_id)
            self._end_echo_risk(generation_id)
            raise
        except ProviderError as error:
            self._telemetry.provider_errors += 1
            self._output.cancel_generation(generation_id)
            self._end_echo_risk(generation_id)
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

    def _report_first_audio(
        self,
        *,
        turn_id: str,
        generation_id: str,
        stages: dict[str, float],
        speech_ms: float,
        turn_queue_ms: float,
    ) -> None:
        """Attribute speech-end to first assistant audio across every stage.

        One event per turn, so a slow reply can be blamed on a stage rather than
        on the pipeline as a whole. The stages before `first_audio_queued_ms`
        sum to it. `turn_queue_ms` is not one of them: it measures a wall-clock
        wait that falls *inside* the endpointing window, and adding it would
        count that time twice.
        """
        for name, milliseconds in stages.items():
            self._telemetry.timing(name, milliseconds)
        self._emit(
            "turn.latency",
            turn_id=turn_id,
            generation_id=generation_id,
            speech_ms=round(speech_ms, 1),
            turn_queue_ms=round(turn_queue_ms, 1),
            **{name: round(milliseconds, 1) for name, milliseconds in stages.items()},
        )
        self._emit(
            "assistant.speaking",
            generation_id=generation_id,
            latency_ms=round(stages["first_audio_queued_ms"], 1),
        )

    async def _cancel_generation(
        self,
        *,
        reason: str,
        notify: bool,
        confirmed_at: float | None = None,
        level_dbfs: float | None = None,
    ) -> str | None:
        generation_id = self._generation_id
        task = self._generation_task
        self._generation_id = None
        if generation_id is not None:
            self._output.cancel_generation(generation_id)
            self._end_echo_risk(generation_id)
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
                # A barge-in far quieter than a talker in front of the microphone
                # is echo the browser's canceller let through, not the user.
                **({} if level_dbfs is None else {"level_dbfs": level_dbfs}),
            )
        return generation_id

    def _begin_echo_risk(self, generation_id: str) -> None:
        self._echo_generation_id = generation_id
        self._echo_risk_until = None

    def _end_echo_risk(self, generation_id: str) -> None:
        if generation_id == self._echo_generation_id:
            self._echo_risk_until = monotonic() + PLAYBACK_TAIL_SECONDS

    def _clear_echo_risk(self, generation_id: str) -> None:
        if generation_id == self._echo_generation_id:
            self._echo_generation_id = None
            self._echo_risk_until = None

    def _active_echo_generation(self, now: float | None = None) -> str | None:
        if self._echo_generation_id is None:
            return None
        current = monotonic() if now is None else now
        if self._echo_risk_until is not None and current >= self._echo_risk_until:
            self._echo_generation_id = None
            self._echo_risk_until = None
            return None
        return self._echo_generation_id

    def _remember_assistant_text(self, generation_id: str, text: str) -> None:
        existing = self._echo_text.get(generation_id, "")
        self._echo_text[generation_id] = f"{existing}{text}"[-ECHO_TEXT_CHARACTERS:]
        while len(self._echo_text) > self._echo_context_limit:
            oldest = next(iter(self._echo_text))
            self._echo_text.pop(oldest)
            self._telemetry.echo_reference_evictions += 1

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
        consumers: Mapping[str, ConsumerProvider],
        default_configuration: VoiceConfiguration | None = None,
        detector_factory: Callable[[VoiceConfiguration], SileroTurnDetector] | None = None,
    ) -> None:
        self.speech = speech
        self.consumers = dict(consumers)
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
            return "retrying"
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
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> ConversationSession:
        if not self._ready:
            raise RuntimeError("conversation services are not ready")
        consumer = self.consumers.get(tenant_id)
        if consumer is None:
            raise RuntimeError(f"no consumer is configured for tenant {tenant_id!r}")
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
            consumer=consumer,
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
        await asyncio.gather(
            self.speech.close(),
            *(consumer.close() for consumer in self.consumers.values()),
            return_exceptions=True,
        )

    async def _warm(self) -> None:
        delay = WARM_RETRY_SECONDS
        while True:
            try:
                await self._warm_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._error = type(error).__name__
                logger.warning(
                    "conversation warm-up failed (%s); retrying in %ds",
                    error,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_WARM_RETRY_SECONDS)
                continue
            self._error = None
            self._ready = True
            logger.info("conversation services ready")
            return

    async def _warm_once(self) -> None:
        # A consumer is someone else's service and restarts on its own schedule.
        # Its absence must not stop Fennec becoming ready, nor hold up the other
        # tenants; that tenant's turns fail loudly instead.
        for tenant_id, consumer in self.consumers.items():
            try:
                await consumer.health()
            except Exception as error:
                logger.warning(
                    "consumer for tenant %s is unreachable; its turns will fail until it returns: %s",
                    tenant_id,
                    error,
                )
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


async def _stamp_first_delta(
    deltas: AsyncIterator[str],
    mark: list[float],
) -> AsyncIterator[str]:
    """Record when the consumer's first token arrived, separating how long it
    thought from how long phrase assembly then held the audio back."""
    async for delta in deltas:
        if not mark:
            mark.append(monotonic())
        yield delta


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


def _is_backchannel(transcript: str) -> bool:
    """Whether a transcript is only listening noises, so it means "go on" rather than
    anything to answer. Hyphens go first: Whisper writes the same sound as "mm-hmm",
    "mmhmm", or "Mm hmm" depending on the phrase around it."""
    sounds = TOKEN_PATTERN.findall(transcript.casefold().replace("-", ""))
    return bool(sounds) and all(sound in BACKCHANNEL_SOUNDS for sound in sounds)


def _is_assistant_echo(transcript: str, assistant_text: str) -> bool:
    transcript_tokens = TOKEN_PATTERN.findall(transcript.casefold())
    if len(transcript_tokens) < MIN_ECHO_TOKENS:
        return False
    assistant_tokens = TOKEN_PATTERN.findall(assistant_text.casefold())
    if len(assistant_tokens) < MIN_ECHO_TOKENS:
        return False
    match = SequenceMatcher(
        None,
        transcript_tokens,
        assistant_tokens,
        autojunk=False,
    ).find_longest_match()
    return (
        match.size >= MIN_ECHO_TOKENS
        and match.size / len(transcript_tokens) >= ECHO_TOKEN_COVERAGE
    )


def _provider_component(error: ProviderError) -> str:
    message = str(error).lower()
    if "stt" in message:
        return "stt"
    if "tts" in message:
        return "tts"
    if "consumer" in message:
        return "consumer"
    return "speech"
