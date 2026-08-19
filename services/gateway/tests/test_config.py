import pytest

from fennec_gateway.config import ConfigurationError, Settings


def test_settings_require_strong_runtime_secrets() -> None:
    with pytest.raises(ConfigurationError, match="SERVICE_TOKEN"):
        Settings(service_token="short", session_secret="x" * 32)

    with pytest.raises(ConfigurationError, match="SESSION_SECRET"):
        Settings(service_token="x" * 24, session_secret="short")


def test_settings_bound_session_lifetime() -> None:
    with pytest.raises(ConfigurationError, match="between 30 and 900"):
        Settings(service_token="x" * 24, session_secret="x" * 32, session_ttl_seconds=901)


def test_settings_bound_session_capacity() -> None:
    with pytest.raises(ConfigurationError, match="MAX_SESSIONS"):
        Settings(service_token="x" * 24, session_secret="x" * 32, max_sessions=0)


def test_settings_bound_turn_detection_configuration() -> None:
    assert Settings(
        service_token="x" * 24,
        session_secret="x" * 32,
    ).endpoint_silence_ms == 1_200

    with pytest.raises(ConfigurationError, match="ENDPOINT_SILENCE"):
        Settings(
            service_token="x" * 24,
            session_secret="x" * 32,
            endpoint_silence_ms=200,
        )

    with pytest.raises(ConfigurationError, match="VAD_THRESHOLD"):
        Settings(
            service_token="x" * 24,
            session_secret="x" * 32,
            vad_threshold=1.0,
        )


def test_settings_require_complete_turn_configuration() -> None:
    with pytest.raises(ConfigurationError, match="must be set together"):
        Settings(
            service_token="x" * 24,
            session_secret="x" * 32,
            turn_url="turn:turn.internal:3478",
        )

    with pytest.raises(ConfigurationError, match="must use turn"):
        Settings(
            service_token="x" * 24,
            session_secret="x" * 32,
            turn_url="https://turn.internal",
            turn_public_url="turn:turn.example:3478",
            turn_shared_secret="x" * 32,
        )

    with pytest.raises(ConfigurationError, match="TURN_SHARED_SECRET"):
        Settings(
            service_token="x" * 24,
            session_secret="x" * 32,
            turn_url="turn:turn.internal:3478?transport=udp",
            turn_public_url="turn:turn.example:3478?transport=udp",
            turn_shared_secret="short",
        )

    settings = Settings(
        service_token="x" * 24,
        session_secret="x" * 32,
        turn_url="turn:turn.internal:3478?transport=udp",
        turn_public_url="turn:turn.example:3478?transport=udp",
        turn_shared_secret="x" * 32,
    )
    assert settings.turn_shared_secret == "x" * 32
