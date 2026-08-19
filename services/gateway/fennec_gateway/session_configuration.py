from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .config import Settings


MODEL_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]*$"
VOICE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
LANGUAGE_PATTERN = r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$"


class VoiceConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    stt_model: str = Field(min_length=1, max_length=256, pattern=MODEL_ID_PATTERN)
    tts_model: str = Field(min_length=1, max_length=256, pattern=MODEL_ID_PATTERN)
    tts_voice: str = Field(min_length=1, max_length=128, pattern=VOICE_ID_PATTERN)
    speech_language: str = Field(min_length=2, max_length=32, pattern=LANGUAGE_PATTERN)
    vad_threshold: float = Field(ge=0.1, le=0.9)
    endpoint_silence_ms: int = Field(ge=300, le=3_000)
    prefix_ms: int = Field(ge=100, le=1_000)
    min_speech_ms: int = Field(ge=80, le=1_000)
    max_turn_seconds: int = Field(ge=5, le=120)

    @classmethod
    def from_settings(cls, settings: Settings) -> VoiceConfiguration:
        return cls(
            stt_model=settings.stt_model,
            tts_model=settings.tts_model,
            tts_voice=settings.tts_voice,
            speech_language=settings.speech_language,
            vad_threshold=settings.vad_threshold,
            endpoint_silence_ms=settings.endpoint_silence_ms,
            prefix_ms=settings.prefix_ms,
            min_speech_ms=settings.min_speech_ms,
            max_turn_seconds=settings.max_turn_seconds,
        )


class VoiceConfigurationOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    stt_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        pattern=MODEL_ID_PATTERN,
    )
    tts_model: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
        pattern=MODEL_ID_PATTERN,
    )
    tts_voice: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=VOICE_ID_PATTERN,
    )
    speech_language: str | None = Field(
        default=None,
        min_length=2,
        max_length=32,
        pattern=LANGUAGE_PATTERN,
    )
    vad_threshold: float | None = Field(default=None, ge=0.1, le=0.9)
    endpoint_silence_ms: int | None = Field(default=None, ge=300, le=3_000)
    prefix_ms: int | None = Field(default=None, ge=100, le=1_000)
    min_speech_ms: int | None = Field(default=None, ge=80, le=1_000)
    max_turn_seconds: int | None = Field(default=None, ge=5, le=120)

    def resolve(self, defaults: VoiceConfiguration) -> VoiceConfiguration:
        return defaults.model_copy(
            update=self.model_dump(exclude_none=True),
        )
