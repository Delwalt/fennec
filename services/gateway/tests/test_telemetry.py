from unittest.mock import patch

from fennec_gateway.telemetry import SessionTelemetry


def test_session_telemetry_is_bounded_and_contains_no_conversation_text() -> None:
    with patch("fennec_gateway.telemetry.monotonic", side_effect=[10.0, 12.5]):
        telemetry = SessionTelemetry(sample_limit=3)
        for value in (10.0, 20.0, 30.0, 40.0):
            telemetry.timing("first_audio_queued_ms", value)
        telemetry.turns_committed = 4
        telemetry.continued_segments = 2
        telemetry.observe_input_queue(3)
        telemetry.observe_input_queue(2)
        summary = telemetry.summary(
            output_queue_peak_frames=7,
            stale_audio_frames_rejected=5,
        )

    assert summary["duration_ms"] == 2_500.0
    assert summary["input_queue_peak_frames"] == 3
    assert summary["continued_segments"] == 2
    assert summary["output_queue_peak_frames"] == 7
    assert summary["stale_audio_frames_rejected"] == 5
    assert summary["latency_ms"]["first_audio_queued_ms"] == {
        "count": 3,
        "median": 30.0,
        "p95": 40.0,
        "max": 40.0,
    }
    assert "text" not in str(summary).lower()
