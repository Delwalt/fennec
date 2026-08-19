from faster_whisper.vad import VadOptions
import numpy as np

from fennec_gateway.turns import SileroTurnDetector


def fake_timestamps(audio: np.ndarray, _: VadOptions) -> list[dict[str, int]]:
    speech = np.flatnonzero(np.abs(audio) > 0.1)
    if speech.size == 0:
        return []
    return [{"start": int(speech[0]), "end": int(speech[-1]) + 1}]


def pcm_chunk(value: int, *, milliseconds: int = 20) -> bytes:
    samples = 16_000 * milliseconds // 1_000
    return np.full(samples, value, dtype="<i2").tobytes()


def test_silero_endpoint_tolerates_short_pauses_and_finalizes_after_silence() -> None:
    detector = SileroTurnDetector(
        endpoint_silence_ms=700,
        timestamp_detector=fake_timestamps,
    )
    events = []
    for _ in range(12):
        events.append(detector.feed(pcm_chunk(8_000)))
    for _ in range(20):
        events.append(detector.feed(pcm_chunk(0)))
    assert any(event.speech_started for event in events)
    assert not any(event.finalized_audio is not None for event in events)

    for _ in range(25):
        events.append(detector.feed(pcm_chunk(0)))

    finalized = [event for event in events if event.finalized_audio is not None]
    assert len(finalized) == 1
    assert finalized[0].forced_by_limit is False
    assert 700 <= finalized[0].speech_end_delay_ms < 900


def test_silero_endpoint_caps_never_ending_turns() -> None:
    detector = SileroTurnDetector(
        max_turn_seconds=1,
        timestamp_detector=fake_timestamps,
    )
    events = [detector.feed(pcm_chunk(8_000)) for _ in range(60)]

    forced = [event for event in events if event.finalized_audio is not None]
    assert len(forced) == 1
    assert forced[0].forced_by_limit is True
    assert forced[0].speech_end_delay_ms == 0


def test_default_endpoint_keeps_a_one_second_thinking_pause_in_one_turn() -> None:
    detector = SileroTurnDetector(timestamp_detector=fake_timestamps)
    events = [detector.feed(pcm_chunk(8_000)) for _ in range(12)]
    events.extend(detector.feed(pcm_chunk(0)) for _ in range(50))

    assert any(event.speech_started for event in events)
    assert not any(event.finalized_audio is not None for event in events)

    events.extend(detector.feed(pcm_chunk(8_000)) for _ in range(12))
    events.extend(detector.feed(pcm_chunk(0)) for _ in range(65))
    assert len([event for event in events if event.finalized_audio is not None]) == 1
