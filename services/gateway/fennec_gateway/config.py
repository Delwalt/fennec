from __future__ import annotations

from dataclasses import dataclass
import os


class ConfigurationError(ValueError):
    """Raised when gateway configuration is unsafe or incomplete."""


def _read_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"invalid boolean value: {value!r}")


@dataclass(frozen=True, slots=True)
class Settings:
    service_token: str
    session_secret: str
    public_base_url: str = "http://127.0.0.1:8080"
    session_ttl_seconds: int = 120
    max_sessions: int = 64
    dev_mode: bool = False
    conversation_enabled: bool = False
    speech_base_url: str = "http://127.0.0.1:8000/v1"
    stt_model: str = "Systran/faster-distil-whisper-small.en"
    tts_model: str = "speaches-ai/Kokoro-82M-v1.0-ONNX"
    tts_voice: str = "af_heart"
    speech_language: str = "en"
    consumer_url: str = "http://127.0.0.1:8090/v1/turns"
    consumer_health_url: str = ""
    consumer_token: str = ""
    provider_timeout_seconds: float = 120.0
    vad_threshold: float = 0.5
    endpoint_silence_ms: int = 1_200
    prefix_ms: int = 320
    min_speech_ms: int = 160
    max_turn_seconds: int = 30
    turn_url: str = ""
    turn_public_url: str = ""
    turn_shared_secret: str = ""
    turn_credential_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if len(self.service_token) < 24:
            raise ConfigurationError("FENNEC_SERVICE_TOKEN must contain at least 24 characters")
        if len(self.session_secret) < 32:
            raise ConfigurationError("FENNEC_SESSION_SECRET must contain at least 32 characters")
        if not 30 <= self.session_ttl_seconds <= 900:
            raise ConfigurationError("FENNEC_SESSION_TTL_SECONDS must be between 30 and 900")
        if not 1 <= self.max_sessions <= 10_000:
            raise ConfigurationError("FENNEC_MAX_SESSIONS must be between 1 and 10000")
        if not self.public_base_url.startswith(("http://", "https://")):
            raise ConfigurationError("FENNEC_PUBLIC_BASE_URL must use http:// or https://")
        if self.conversation_enabled:
            if not self.speech_base_url.startswith(("http://", "https://")):
                raise ConfigurationError("FENNEC_SPEECH_BASE_URL must use http:// or https://")
            if not self.consumer_url.startswith(("http://", "https://")):
                raise ConfigurationError("FENNEC_CONSUMER_URL must use http:// or https://")
            if self.consumer_health_url and not self.consumer_health_url.startswith(("http://", "https://")):
                raise ConfigurationError("FENNEC_CONSUMER_HEALTH_URL must use http:// or https://")
            if len(self.consumer_token) < 24:
                raise ConfigurationError(
                    "FENNEC_CONSUMER_TOKEN must contain at least 24 characters when conversation is enabled"
                )
        if not 5 <= self.provider_timeout_seconds <= 600:
            raise ConfigurationError("FENNEC_PROVIDER_TIMEOUT_SECONDS must be between 5 and 600")
        if not 0.1 <= self.vad_threshold <= 0.9:
            raise ConfigurationError("FENNEC_VAD_THRESHOLD must be between 0.1 and 0.9")
        if not 300 <= self.endpoint_silence_ms <= 3_000:
            raise ConfigurationError("FENNEC_ENDPOINT_SILENCE_MS must be between 300 and 3000")
        if not 100 <= self.prefix_ms <= 1_000:
            raise ConfigurationError("FENNEC_PREFIX_MS must be between 100 and 1000")
        if not 80 <= self.min_speech_ms <= 1_000:
            raise ConfigurationError("FENNEC_MIN_SPEECH_MS must be between 80 and 1000")
        if not 5 <= self.max_turn_seconds <= 120:
            raise ConfigurationError("FENNEC_MAX_TURN_SECONDS must be between 5 and 120")
        turn_values = (self.turn_url, self.turn_public_url, self.turn_shared_secret)
        if any(turn_values) and not all(turn_values):
            raise ConfigurationError(
                "FENNEC_TURN_URL, FENNEC_TURN_PUBLIC_URL, and FENNEC_TURN_SHARED_SECRET must be set together"
            )
        if self.turn_url and not self.turn_url.startswith(("turn:", "turns:")):
            raise ConfigurationError("FENNEC_TURN_URL must use turn: or turns:")
        if self.turn_public_url and not self.turn_public_url.startswith(("turn:", "turns:")):
            raise ConfigurationError("FENNEC_TURN_PUBLIC_URL must use turn: or turns:")
        if self.turn_shared_secret and len(self.turn_shared_secret) < 32:
            raise ConfigurationError("FENNEC_TURN_SHARED_SECRET must contain at least 32 characters")
        if not 30 <= self.turn_credential_ttl_seconds <= 3_600:
            raise ConfigurationError("FENNEC_TURN_CREDENTIAL_TTL_SECONDS must be between 30 and 3600")

    @classmethod
    def from_env(cls) -> Settings:
        raw_ttl = os.getenv("FENNEC_SESSION_TTL_SECONDS", "120")
        try:
            ttl = int(raw_ttl)
        except ValueError as error:
            raise ConfigurationError("FENNEC_SESSION_TTL_SECONDS must be an integer") from error
        raw_max_sessions = os.getenv("FENNEC_MAX_SESSIONS", "64")
        try:
            max_sessions = int(raw_max_sessions)
        except ValueError as error:
            raise ConfigurationError("FENNEC_MAX_SESSIONS must be an integer") from error
        raw_provider_timeout = os.getenv("FENNEC_PROVIDER_TIMEOUT_SECONDS", "120")
        try:
            provider_timeout = float(raw_provider_timeout)
        except ValueError as error:
            raise ConfigurationError("FENNEC_PROVIDER_TIMEOUT_SECONDS must be numeric") from error
        try:
            vad_threshold = float(os.getenv("FENNEC_VAD_THRESHOLD", "0.5"))
            endpoint_silence_ms = int(os.getenv("FENNEC_ENDPOINT_SILENCE_MS", "1200"))
            prefix_ms = int(os.getenv("FENNEC_PREFIX_MS", "320"))
            min_speech_ms = int(os.getenv("FENNEC_MIN_SPEECH_MS", "160"))
            max_turn_seconds = int(os.getenv("FENNEC_MAX_TURN_SECONDS", "30"))
            turn_credential_ttl_seconds = int(os.getenv("FENNEC_TURN_CREDENTIAL_TTL_SECONDS", "300"))
        except ValueError as error:
            raise ConfigurationError("FENNEC turn settings must be numeric") from error

        return cls(
            service_token=os.getenv("FENNEC_SERVICE_TOKEN", ""),
            session_secret=os.getenv("FENNEC_SESSION_SECRET", ""),
            public_base_url=os.getenv("FENNEC_PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
            session_ttl_seconds=ttl,
            max_sessions=max_sessions,
            dev_mode=_read_bool(os.getenv("FENNEC_DEV_MODE")),
            conversation_enabled=_read_bool(os.getenv("FENNEC_CONVERSATION_ENABLED")),
            speech_base_url=os.getenv(
                "FENNEC_SPEECH_BASE_URL", "http://127.0.0.1:8000/v1"
            ).rstrip("/"),
            stt_model=os.getenv(
                "FENNEC_STT_MODEL", "Systran/faster-distil-whisper-small.en"
            ),
            tts_model=os.getenv(
                "FENNEC_TTS_MODEL", "speaches-ai/Kokoro-82M-v1.0-ONNX"
            ),
            tts_voice=os.getenv("FENNEC_TTS_VOICE", "af_heart"),
            speech_language=os.getenv("FENNEC_SPEECH_LANGUAGE", "en"),
            consumer_url=os.getenv(
                "FENNEC_CONSUMER_URL", "http://127.0.0.1:8090/v1/turns"
            ),
            consumer_health_url=os.getenv("FENNEC_CONSUMER_HEALTH_URL", "").strip(),
            consumer_token=os.getenv("FENNEC_CONSUMER_TOKEN", ""),
            provider_timeout_seconds=provider_timeout,
            vad_threshold=vad_threshold,
            endpoint_silence_ms=endpoint_silence_ms,
            prefix_ms=prefix_ms,
            min_speech_ms=min_speech_ms,
            max_turn_seconds=max_turn_seconds,
            turn_url=os.getenv("FENNEC_TURN_URL", "").strip(),
            turn_public_url=os.getenv("FENNEC_TURN_PUBLIC_URL", "").strip(),
            turn_shared_secret=os.getenv("FENNEC_TURN_SHARED_SECRET", "").strip(),
            turn_credential_ttl_seconds=turn_credential_ttl_seconds,
        )
