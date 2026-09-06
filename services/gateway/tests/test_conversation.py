import asyncio
from collections.abc import AsyncIterator
from io import BytesIO
from unittest.mock import patch
import wave

import pytest

from conftest import wait_until
from fennec_gateway import conversation
from fennec_gateway.conversation import (
    ConversationRuntime,
    ConversationSession,
    MAX_CONTINUED_SEGMENTS,
    response_phrases,
)
from fennec_gateway.media import AssistantAudioTrack
from fennec_gateway.providers import FinalizedTurn, ProviderError
from fennec_gateway.session_configuration import VoiceConfiguration
from fennec_gateway.turns import TurnDetection


class ScriptedDetector:
    def __init__(self) -> None:
        self.calls = 0

    def feed(self, _: bytes) -> TurnDetection:
        self.calls += 1
        if self.calls == 1:
            return TurnDetection(speech_started=True)
        return TurnDetection(finalized_audio=bytes(6_400))

    def reset(self) -> None:
        pass


class FakeSpeech:
    def __init__(self) -> None:
        self.transcriptions = 0
        self.synthesized: list[str] = []

    async def ensure_models(self) -> None:
        pass

    async def transcribe(self, _: bytes) -> str:
        self.transcriptions += 1
        return "What is on my calendar?"

    async def synthesize(self, text: str) -> bytes:
        self.synthesized.append(text)
        output = BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(bytes(960))
        return output.getvalue()

    async def close(self) -> None:
        pass


class FakeConsumer:
    async def health(self) -> None:
        pass

    async def respond(self, _: FinalizedTurn) -> AsyncIterator[str]:
        yield "You have two meetings. "
        yield "The first starts at ten."

    async def close(self) -> None:
        pass


class BargeInDetector:
    def __init__(self) -> None:
        self.calls = 0

    def feed(self, _: bytes) -> TurnDetection:
        self.calls += 1
        if self.calls == 1:
            return TurnDetection(finalized_audio=bytes(6_400))
        return TurnDetection(
            speech_started=True,
            candidate_active=True,
            candidate_evaluated=True,
            speech_duration_ms=200,
            speech_level_dbfs=-20,
            recent_speech_level_dbfs=-20,
        )

    def reset(self) -> None:
        pass


class BlockingSpeech(FakeSpeech):
    async def synthesize(self, text: str) -> bytes:
        self.synthesized.append(text)
        await asyncio.Future()
        raise AssertionError("unreachable")


class FalseInterruptionDetector:
    def __init__(self) -> None:
        self.calls = 0

    def feed(self, _: bytes) -> TurnDetection:
        self.calls += 1
        if self.calls in {1, 3}:
            return TurnDetection(finalized_audio=bytes(6_400))
        return TurnDetection(speech_started=True)

    def reset(self) -> None:
        pass


class EmptyAfterInterruptionSpeech(BlockingSpeech):
    async def transcribe(self, _: bytes) -> str:
        self.transcriptions += 1
        return "Start a response" if self.transcriptions == 1 else ""


class ContinuedSpeechDetector:
    def __init__(self) -> None:
        self.calls = 0

    def feed(self, _: bytes) -> TurnDetection:
        self.calls += 1
        if self.calls == 1:
            return TurnDetection(finalized_audio=bytes(6_400))
        if self.calls == 2:
            return TurnDetection(speech_started=True)
        return TurnDetection(finalized_audio=bytes(6_400))

    def reset(self) -> None:
        pass


class CoordinatedSpeech(FakeSpeech):
    def __init__(self) -> None:
        super().__init__()
        self.first_transcription_started = asyncio.Event()
        self.release_first_transcription = asyncio.Event()

    async def transcribe(self, _: bytes) -> str:
        self.transcriptions += 1
        if self.transcriptions == 1:
            self.first_transcription_started.set()
            await self.release_first_transcription.wait()
            return "This is my first sentence."
        return "This is my second sentence."


class RecordingConsumer(FakeConsumer):
    def __init__(self) -> None:
        self.turns: list[FinalizedTurn] = []

    async def respond(self, turn: FinalizedTurn) -> AsyncIterator[str]:
        self.turns.append(turn)
        yield "I heard both sentences."


class RelentlessDetector:
    """Every feed finalizes a turn, and announces fresh speech first — the shape of someone
    who never stops talking, which is what starved the continuation chain."""

    def __init__(self) -> None:
        self.calls = 0

    def feed(self, _: bytes) -> TurnDetection:
        self.calls += 1
        if self.calls % 2:
            return TurnDetection(speech_started=True)
        return TurnDetection(finalized_audio=bytes(6_400))

    def reset(self) -> None:
        pass


class SlowNumberedSpeech(FakeSpeech):
    """Transcription that takes long enough for the next turn to arrive behind it, which is
    the condition a continuation is requested under."""

    async def transcribe(self, _: bytes) -> str:
        self.transcriptions += 1
        await asyncio.sleep(0.05)
        return f"segment {self.transcriptions}"


class ConfigurableSpeech(FakeSpeech):
    def __init__(self) -> None:
        super().__init__()
        self.transcription_configuration: tuple[str | None, str | None] | None = None
        self.synthesis_configuration: list[tuple[str | None, str | None]] = []

    async def transcribe(
        self,
        _: bytes,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> str:
        self.transcriptions += 1
        self.transcription_configuration = (model, language)
        return "Use this configured voice."

    async def synthesize(
        self,
        text: str,
        *,
        model: str | None = None,
        voice: str | None = None,
    ) -> bytes:
        self.synthesis_configuration.append((model, voice))
        return await super().synthesize(text)


class SequenceDetector:
    def __init__(self, detections: list[TurnDetection]) -> None:
        self._detections = iter(detections)

    def feed(self, _: bytes) -> TurnDetection:
        return next(self._detections, TurnDetection())

    def reset(self) -> None:
        pass


def candidate(*, level_dbfs: float, started: bool = False) -> TurnDetection:
    return TurnDetection(
        speech_started=started,
        candidate_active=True,
        candidate_evaluated=True,
        speech_duration_ms=200,
        speech_level_dbfs=level_dbfs,
        recent_speech_level_dbfs=level_dbfs,
    )


def finalized_candidate(*, level_dbfs: float) -> TurnDetection:
    return TurnDetection(
        candidate_evaluated=True,
        speech_duration_ms=300,
        speech_level_dbfs=level_dbfs,
        recent_speech_level_dbfs=level_dbfs,
        finalized_audio=bytes(6_400),
    )


class EchoSpeech(FakeSpeech):
    async def transcribe(self, _: bytes) -> str:
        self.transcriptions += 1
        if self.transcriptions == 1:
            return "Check the server."
        return "The server is healthy and ready."


class EchoConsumer(RecordingConsumer):
    async def respond(self, turn: FinalizedTurn) -> AsyncIterator[str]:
        self.turns.append(turn)
        yield "The server is healthy and ready."


class ScriptedSpeech(FakeSpeech):
    """Answers the first turn, then says whatever the barge-in was meant to be."""

    def __init__(self, interruption: str) -> None:
        super().__init__()
        self._interruption = interruption

    async def transcribe(self, _: bytes) -> str:
        self.transcriptions += 1
        return "Check the server." if self.transcriptions == 1 else self._interruption


class LongAudioSpeech(FakeSpeech):
    async def synthesize(self, text: str) -> bytes:
        self.synthesized.append(text)
        output = BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24_000)
            wav.writeframes(bytes(96_000))
        return output.getvalue()


async def wait_for_event(events: list[tuple[str, dict]], event_type: str) -> dict:
    async with asyncio.timeout(3):
        while True:
            for name, data in events:
                if name == event_type:
                    return data
            await asyncio.sleep(0.01)


async def test_complete_turn_emits_final_transcript_and_queues_streamed_speech() -> None:
    events: list[tuple[str, dict]] = []
    speech = FakeSpeech()
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=ScriptedDetector(),  # type: ignore[arg-type]
    )
    conversation.feed_audio(bytes(640))
    conversation.feed_audio(bytes(640))

    await wait_for_event(events, "assistant.done")
    await conversation.close()
    output.stop()

    transcripts = [data for event, data in events if event == "transcript.final"]
    assert [event for event, _ in events].count("transcript.final") == 1
    assert transcripts[0]["text"] == "What is on my calendar?"
    assert speech.transcriptions == 1
    assert speech.synthesized == ["You have two meetings. ", "The first starts at ten."]
    assert output.queued_frames > 0
    summaries = [data for event, data in events if event == "telemetry.session.summary"]
    assert summaries[0]["turns_committed"] == 1
    assert summaries[0]["generations_completed"] == 1
    assert summaries[0]["latency_ms"]["first_audio_queued_ms"]["count"] == 1


async def test_speech_after_a_pause_preserves_the_transcript_already_in_flight() -> None:
    events: list[tuple[str, dict]] = []
    speech = CoordinatedSpeech()
    consumer = RecordingConsumer()
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=consumer,
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=ContinuedSpeechDetector(),  # type: ignore[arg-type]
    )

    conversation.feed_audio(bytes(640))
    await speech.first_transcription_started.wait()
    conversation.feed_audio(bytes(640))
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "speech.started")
    speech.release_first_transcription.set()

    await wait_for_event(events, "assistant.done")
    await conversation.close()
    output.stop()

    combined = "This is my first sentence. This is my second sentence."
    transcripts = [data for event, data in events if event == "transcript.final"]
    assert [data["text"] for data in transcripts] == [combined]
    assert [turn.text for turn in consumer.turns] == [combined]
    assert speech.transcriptions == 2
    assert [event for event, _ in events].count("turn.deferred") == 1
    assert not any(event == "assistant.cancelled" for event, _ in events)
    summaries = [data for event, data in events if event == "telemetry.session.summary"]
    assert summaries[0]["continued_segments"] == 1


async def test_a_deferred_turn_leaves_the_session_listening() -> None:
    # The client reads `transcribing` as thinking. A deferral that never says otherwise
    # leaves it saying Dex is thinking while the gateway waits for the next word.
    events: list[tuple[str, dict]] = []
    speech = CoordinatedSpeech()
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=RecordingConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=ContinuedSpeechDetector(),  # type: ignore[arg-type]
    )

    conversation.feed_audio(bytes(640))
    await speech.first_transcription_started.wait()
    conversation.feed_audio(bytes(640))
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "speech.started")
    speech.release_first_transcription.set()
    await wait_for_event(events, "assistant.done")
    await conversation.close()
    output.stop()

    ordered = [event for event, _ in events]
    deferred = ordered.index("turn.deferred")
    assert ordered[deferred + 1] == "state.changed"
    assert events[deferred + 1][1]["state"] == "listening"


async def test_an_unbroken_talker_still_gets_an_answer() -> None:
    # Continuations are requested by speech starting and released by a transcription
    # finishing. Speak steadily enough and the first outruns the second, every turn is
    # deferred, and the consumer is never asked anything at all.
    events: list[tuple[str, dict]] = []
    consumer = RecordingConsumer()
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=SlowNumberedSpeech(),
        consumer=consumer,
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=RelentlessDetector(),  # type: ignore[arg-type]
    )

    # Faster than transcription, so there is always a turn behind the one being transcribed
    # when the next phrase starts — which is what asks for a continuation.
    async def keep_talking() -> None:
        for _ in range(40):
            try:
                conversation.feed_audio(bytes(640))
            except Exception:  # the bounded queue pushing back is itself the speaker pausing
                pass
            await asyncio.sleep(0.005)

    talking = asyncio.create_task(keep_talking())
    try:
        await wait_until(lambda: bool(consumer.turns), timeout=3.0)
    finally:
        talking.cancel()
        await asyncio.gather(talking, return_exceptions=True)
    await conversation.close()
    output.stop()

    # The bound is on the chain, not on the conversation: what was deferred is folded into
    # the answer rather than dropped.
    assert consumer.turns, "a turn must reach the consumer however long the speaker runs on"
    assert "segment 1" in consumer.turns[0].text
    deferrals = [event for event, _ in events].count("turn.deferred")
    assert deferrals <= MAX_CONTINUED_SEGMENTS


async def test_session_speech_configuration_reaches_stt_and_tts() -> None:
    events: list[tuple[str, dict]] = []
    speech = ConfigurableSpeech()
    output = AssistantAudioTrack()
    configuration = VoiceConfiguration(
        stt_model="custom-whisper",
        tts_model="custom-kokoro",
        tts_voice="custom_voice",
        speech_language="en-IN",
        vad_threshold=0.45,
        endpoint_silence_ms=1_500,
        prefix_ms=400,
        min_speech_ms=200,
        max_turn_seconds=45,
    )
    conversation = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=ScriptedDetector(),  # type: ignore[arg-type]
        configuration=configuration,
    )
    conversation.feed_audio(bytes(640))
    conversation.feed_audio(bytes(640))

    await wait_for_event(events, "assistant.done")
    await conversation.close()
    output.stop()

    assert speech.transcription_configuration == ("custom-whisper", "en-IN")
    assert speech.synthesis_configuration == [
        ("custom-kokoro", "custom_voice"),
        ("custom-kokoro", "custom_voice"),
    ]


async def test_response_phrases_flushes_punctuation_and_bounded_delay() -> None:
    async def deltas() -> AsyncIterator[str]:
        yield "First sentence. Next"
        await asyncio.sleep(0.03)
        yield " phrase"

    phrases = [
        phrase
        async for phrase in response_phrases(deltas(), max_delay_seconds=0.01)
    ]
    assert phrases == ["First sentence. ", "Next", " phrase"]


async def test_response_phrases_preserves_long_punctuation_free_text_order() -> None:
    async def deltas() -> AsyncIterator[str]:
        yield "one two three four "
        yield "five six seven eight"

    phrases = [
        phrase
        async for phrase in response_phrases(
            deltas(),
            max_delay_seconds=1,
            max_characters=18,
        )
    ]
    assert "".join(phrases) == "one two three four five six seven eight"
    assert all(len(phrase) <= 23 for phrase in phrases)


async def test_new_user_speech_cancels_the_active_generation_immediately() -> None:
    events: list[tuple[str, dict]] = []
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=BlockingSpeech(),
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=BargeInDetector(),  # type: ignore[arg-type]
    )
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.text.delta")
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")

    cancelled = [data for event, data in events if event == "assistant.cancelled"]
    assert cancelled[0]["reason"] == "user_speech"
    assert cancelled[0]["latency_ms"] < 200
    assert output.queued_frames == 0
    event_names = [event for event, _ in events]
    assert event_names.index("speech.started") < event_names.index("assistant.cancelled")

    await conversation.close()
    output.stop()
    summaries = [data for event, data in events if event == "telemetry.session.summary"]
    assert summaries[0]["interruptions"] == 1
    assert summaries[0]["latency_ms"]["interruption_cancel_ms"]["count"] == 1


async def test_quiet_echo_is_deferred_and_ignored_without_cancelling() -> None:
    events: list[tuple[str, dict]] = []
    speech = EchoSpeech()
    consumer = EchoConsumer()
    output = AssistantAudioTrack()
    detector = SequenceDetector([
        TurnDetection(finalized_audio=bytes(6_400)),
        candidate(level_dbfs=-55, started=True),
        finalized_candidate(level_dbfs=-55),
    ])
    session = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=consumer,
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=detector,  # type: ignore[arg-type]
    )
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.speaking")
    session.feed_audio(bytes(640))
    session.feed_audio(bytes(640))

    async with asyncio.timeout(3):
        while not any(
            event == "turn.ignored" and data.get("reason") == "assistant_echo"
            for event, data in events
        ):
            await asyncio.sleep(0.01)

    assert not any(event == "assistant.cancelled" for event, _ in events)
    assert [turn.text for turn in consumer.turns] == ["Check the server."]
    assert speech.transcriptions == 2

    await session.close()
    output.stop()
    summary = [data for event, data in events if event == "telemetry.session.summary"][0]
    assert summary["echo_candidates_deferred"] == 1
    assert summary["assistant_echo_turns"] == 1
    assert summary["possible_false_interruptions"] == 0


async def test_pending_candidate_can_strengthen_and_cancel_without_losing_its_turn() -> None:
    events: list[tuple[str, dict]] = []
    speech = FakeSpeech()
    consumer = RecordingConsumer()
    output = AssistantAudioTrack()
    detector = SequenceDetector([
        TurnDetection(finalized_audio=bytes(6_400)),
        candidate(level_dbfs=-55, started=True),
        candidate(level_dbfs=-30),
        finalized_candidate(level_dbfs=-30),
    ])
    session = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=consumer,
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=detector,  # type: ignore[arg-type]
    )
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.speaking")
    session.feed_audio(bytes(640))
    await asyncio.sleep(0)
    assert not any(event == "assistant.cancelled" for event, _ in events)

    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")
    session.feed_audio(bytes(640))
    async with asyncio.timeout(3):
        while speech.transcriptions < 2:
            await asyncio.sleep(0.01)

    assert [event for event, _ in events].count("speech.started") == 1
    assert speech.transcriptions == 2
    assert len(consumer.turns) == 2

    await session.close()
    output.stop()


async def barge_in_saying(spoken: str) -> tuple[list[tuple[str, dict]], RecordingConsumer]:
    events: list[tuple[str, dict]] = []
    consumer = RecordingConsumer()
    output = AssistantAudioTrack()
    detector = SequenceDetector([
        TurnDetection(finalized_audio=bytes(6_400)),
        candidate(level_dbfs=-25, started=True),
        finalized_candidate(level_dbfs=-25),
    ])
    session = ConversationSession(
        session_id="session",
        speech=ScriptedSpeech(spoken),
        consumer=consumer,
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=detector,  # type: ignore[arg-type]
    )
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.speaking")
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")
    session.feed_audio(bytes(640))
    async with asyncio.timeout(3):
        while len(consumer.turns) < 2 and not any(
            event == "turn.ignored" for event, _ in events
        ):
            await asyncio.sleep(0.01)
    await session.close()
    output.stop()
    return events, consumer


async def test_a_hum_that_stopped_the_reply_is_not_answered() -> None:
    events, consumer = await barge_in_saying("Hmm.")

    assert any(
        event == "turn.ignored" and data.get("reason") == "backchannel"
        for event, data in events
    )
    assert [turn.text for turn in consumer.turns] == ["Check the server."]
    summary = [data for event, data in events if event == "telemetry.session.summary"][0]
    assert summary["backchannel_turns"] == 1
    # It stopped a reply for a listening noise, which is a false interruption.
    assert summary["possible_false_interruptions"] == 1


async def test_short_answers_that_stopped_the_reply_still_reach_the_consumer() -> None:
    """"stop" and "wait" are the same length as a hum and the most urgent things a
    speaker says. Only wordless sounds may be dropped."""
    for spoken in ("Stop.", "Wait!", "Yes.", "No."):
        events, consumer = await barge_in_saying(spoken)
        assert not any(event == "turn.ignored" for event, _ in events), spoken
        assert [turn.text for turn in consumer.turns] == ["Check the server.", spoken]


async def test_a_cancelled_reply_that_yields_no_speech_at_all_is_reported() -> None:
    """Fennec cannot resume audio it binned, so the consumer has to hear that its reply
    was stopped for nothing and decide whether to carry on."""
    events: list[tuple[str, dict]] = []
    output = AssistantAudioTrack()
    detector = SequenceDetector([
        TurnDetection(finalized_audio=bytes(6_400)),
        candidate(level_dbfs=-25, started=True),
        TurnDetection(candidate_evaluated=True),
    ])
    session = ConversationSession(
        session_id="session",
        speech=FakeSpeech(),
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=detector,  # type: ignore[arg-type]
    )
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.speaking")
    session.feed_audio(bytes(640))
    cancelled = await wait_for_event(events, "assistant.cancelled")
    session.feed_audio(bytes(640))
    abandoned = await wait_for_event(events, "speech.abandoned")
    assert abandoned["generation_id"] == cancelled["generation_id"]

    await session.close()
    output.stop()
    summary = [data for event, data in events if event == "telemetry.session.summary"][0]
    assert summary["abandoned_after_cancellation"] == 1
    assert summary["possible_false_interruptions"] == 1


async def test_abandoned_candidate_does_not_block_the_next_one() -> None:
    """The detector drops a candidate it stops recognizing as speech. Its context has to
    go with it, or the next candidate is judged against a window that already closed."""
    events: list[tuple[str, dict]] = []
    output = AssistantAudioTrack()
    detector = SequenceDetector([
        TurnDetection(finalized_audio=bytes(6_400)),
        candidate(level_dbfs=-55, started=True),
        TurnDetection(candidate_evaluated=True),
        candidate(level_dbfs=-25, started=True),
    ])
    session = ConversationSession(
        session_id="session",
        speech=FakeSpeech(),
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=detector,  # type: ignore[arg-type]
    )
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.speaking")
    session.feed_audio(bytes(640))
    session.feed_audio(bytes(640))
    await asyncio.sleep(0.05)
    assert not any(event == "assistant.cancelled" for event, _ in events)

    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")

    await session.close()
    output.stop()


async def test_confirmed_interruption_drops_deferred_echo_before_queue_backpressure() -> None:
    events: list[tuple[str, dict]] = []
    speech = LongAudioSpeech()
    output = AssistantAudioTrack()
    detector = SequenceDetector([
        TurnDetection(finalized_audio=bytes(6_400)),
        candidate(level_dbfs=-55, started=True),
        finalized_candidate(level_dbfs=-55),
        candidate(level_dbfs=-55, started=True),
        finalized_candidate(level_dbfs=-55),
        candidate(level_dbfs=-25, started=True),
        finalized_candidate(level_dbfs=-25),
    ])
    session = ConversationSession(
        session_id="session",
        speech=speech,
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=detector,  # type: ignore[arg-type]
        turn_queue_size=1,
    )
    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.speaking")
    for _ in range(4):
        session.feed_audio(bytes(640))
    await asyncio.sleep(0.05)

    session.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")
    session.feed_audio(bytes(640))
    async with asyncio.timeout(3):
        while speech.transcriptions < 2:
            await asyncio.sleep(0.01)

    assert not any(
        event == "error" and data.get("code") in {"turn_backpressure", "turn_detection_failed"}
        for event, data in events
    )
    await session.close()
    output.stop()
    summary = [data for event, data in events if event == "telemetry.session.summary"][0]
    assert summary["dropped_unconfirmed_turns"] == {
        "capacity": 1,
        "generation_cancelled": 1,
    }


async def test_empty_turn_after_interruption_is_counted_as_a_possible_false_interruption() -> None:
    events: list[tuple[str, dict]] = []
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=EmptyAfterInterruptionSpeech(),
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=FalseInterruptionDetector(),  # type: ignore[arg-type]
    )
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.text.delta")
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "turn.ignored")
    await conversation.close()
    output.stop()

    summaries = [data for event, data in events if event == "telemetry.session.summary"]
    assert summaries[0]["possible_false_interruptions"] == 1
    assert summaries[0]["empty_transcripts"] == 1


class UnreachableConsumer(FakeConsumer):
    async def health(self) -> None:
        raise ProviderError("consumer service is unavailable")


class BrokenThenWorkingSpeech(FakeSpeech):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining_failures = failures

    async def ensure_models(self, **_: object) -> None:
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise ProviderError("speech service is unavailable")


async def test_a_dead_consumer_at_boot_still_lets_the_gateway_become_ready() -> None:
    # Regression: the consumer is a third-party service. When it was unreachable at
    # boot the gateway used to stay un-ready forever and refuse every session with 503.
    runtime = ConversationRuntime(speech=FakeSpeech(), consumers={"default": UnreachableConsumer()})
    with patch.object(conversation.SileroTurnDetector, "warm"):
        runtime.start()
        await wait_until(lambda: runtime.ready)

    assert runtime.status == "ready"
    await runtime.close()


async def test_warm_up_retries_until_its_own_speech_dependency_returns() -> None:
    # Regression: a single warm-up failure used to be terminal, so a gateway that
    # started before its speech models were reachable never recovered on its own.
    speech = BrokenThenWorkingSpeech(failures=2)
    runtime = ConversationRuntime(speech=speech, consumers={"default": FakeConsumer()})
    with patch.object(conversation, "WARM_RETRY_SECONDS", 0), \
            patch.object(conversation, "MAX_WARM_RETRY_SECONDS", 0), \
            patch.object(conversation.SileroTurnDetector, "warm"):
        runtime.start()
        await wait_until(lambda: runtime.ready)

    assert speech.remaining_failures == 0
    await runtime.close()


async def test_persistent_speech_failure_keeps_retrying_instead_of_giving_up() -> None:
    # A gateway that cannot reach its own speech models must report that it is still
    # trying, not settle into a terminal state that only a manual restart clears.
    speech = BrokenThenWorkingSpeech(failures=1_000)
    runtime = ConversationRuntime(speech=speech, consumers={"default": FakeConsumer()})
    with patch.object(conversation, "WARM_RETRY_SECONDS", 0), \
            patch.object(conversation, "MAX_WARM_RETRY_SECONDS", 0), \
            patch.object(conversation.SileroTurnDetector, "warm"):
        runtime.start()
        await wait_until(lambda: speech.remaining_failures < 998)
        attempted = 1_000 - speech.remaining_failures
        assert runtime.status == "retrying"
        assert not runtime.ready
        await wait_until(lambda: speech.remaining_failures < attempted)

    await runtime.close()


async def test_speech_during_the_unplayed_tail_is_a_barge_in_not_a_new_turn() -> None:
    events: list[tuple[str, dict]] = []
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=FakeSpeech(),
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=BargeInDetector(),  # type: ignore[arg-type]
    )
    # Nothing plays this track, so every synthesized phrase is still queued when
    # the last one is enqueued - the window the assistant is audibly speaking in.
    conversation.feed_audio(bytes(640))
    async with asyncio.timeout(3):
        while [event for event, _ in events].count("assistant.text.delta") < 2:
            await asyncio.sleep(0.01)
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.cancelled")

    event_names = [event for event, _ in events]
    assert "assistant.done" not in event_names
    assert event_names.index("assistant.cancelled") < len(event_names)
    cancelled = [data for event, data in events if event == "assistant.cancelled"]
    assert cancelled[0]["reason"] == "user_speech"
    assert cancelled[0]["level_dbfs"] <= 0

    await conversation.close()
    output.stop()


async def test_turn_latency_attributes_every_stage_of_the_first_audio() -> None:
    events: list[tuple[str, dict]] = []
    output = AssistantAudioTrack()
    conversation = ConversationSession(
        session_id="session",
        speech=FakeSpeech(),
        consumer=FakeConsumer(),
        output=output,
        send_event=lambda event_type, data: events.append((event_type, data)),
        detector=ScriptedDetector(),  # type: ignore[arg-type]
    )
    conversation.feed_audio(bytes(640))
    conversation.feed_audio(bytes(640))
    await wait_for_event(events, "assistant.done")
    await conversation.close()
    output.stop()

    latency = [data for event, data in events if event == "turn.latency"][0]
    stages = (
        "endpoint_delay_ms",
        "stt_ms",
        "llm_first_delta_ms",
        "phrase_ms",
        "tts_ms",
        "decode_ms",
        "enqueue_ms",
    )
    assert all(latency[stage] >= 0 for stage in stages)
    assert sum(latency[stage] for stage in stages) == pytest.approx(
        latency["first_audio_queued_ms"], abs=5
    )
    assert latency["speech_ms"] > 0
    assert "text" not in latency


async def test_each_tenants_turns_go_only_to_that_tenants_consumer() -> None:
    dex = FakeConsumer()
    teamx = FakeConsumer()
    runtime = ConversationRuntime(speech=FakeSpeech(), consumers={"dex": dex, "teamx": teamx})
    with patch.object(conversation.SileroTurnDetector, "warm"):
        runtime.start()
        await wait_until(lambda: runtime.ready)

    session = runtime.create_session(
        session_id="s-1",
        output=AssistantAudioTrack(),
        send_event=lambda *_: None,
        tenant_id="teamx",
    )
    assert session._consumer is teamx

    with pytest.raises(RuntimeError, match="no consumer is configured for tenant"):
        runtime.create_session(
            session_id="s-2",
            output=AssistantAudioTrack(),
            send_event=lambda *_: None,
            tenant_id="stranger",
        )

    await session.close()
    await runtime.close()
