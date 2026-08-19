from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
from typing import Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from .audio import pcm16_mono_wav


class ProviderError(RuntimeError):
    """A bounded error at a speech or consumer provider boundary."""


class SpeechProvider(Protocol):
    async def ensure_models(
        self,
        *,
        stt_model: str | None = None,
        tts_model: str | None = None,
    ) -> None: ...

    async def transcribe(
        self,
        pcm: bytes,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> str: ...

    async def synthesize(
        self,
        text: str,
        *,
        model: str | None = None,
        voice: str | None = None,
    ) -> bytes: ...

    async def close(self) -> None: ...


class ConsumerProvider(Protocol):
    async def health(self) -> None: ...

    def respond(self, turn: FinalizedTurn) -> AsyncIterator[str]: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FinalizedTurn:
    session_id: str
    turn_id: str
    generation_id: str
    text: str


class LocalSpeechProvider:
    def __init__(
        self,
        *,
        base_url: str,
        stt_model: str,
        tts_model: str,
        voice: str,
        language: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
        max_audio_bytes: int = 24 * 1024 * 1024,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._stt_model = stt_model
        self._tts_model = tts_model
        self._voice = voice
        self._language = language
        self._max_audio_bytes = max_audio_bytes
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        )

    async def ensure_models(
        self,
        *,
        stt_model: str | None = None,
        tts_model: str | None = None,
    ) -> None:
        response = await self._request("GET", "/models")
        try:
            payload = response.json()
        except ValueError as error:
            raise ProviderError("local speech model list was invalid") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ProviderError("local speech model list was invalid")
        installed = {
            candidate.get("id")
            for candidate in payload.get("data", [])
            if isinstance(candidate, dict)
        }
        for model in (stt_model or self._stt_model, tts_model or self._tts_model):
            if model not in installed:
                await self._request("POST", f"/models/{quote(model, safe='')}")

    async def transcribe(
        self,
        pcm: bytes,
        *,
        model: str | None = None,
        language: str | None = None,
    ) -> str:
        wav = pcm16_mono_wav(pcm)
        try:
            response = await self._client.post(
                f"{self._base_url}/audio/transcriptions",
                data={
                    "model": model or self._stt_model,
                    "language": language or self._language,
                    "response_format": "json",
                },
                files={"file": ("turn.wav", wav, "audio/wav")},
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ProviderError("local STT request failed") from error
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise ProviderError("local STT response did not contain text")
        return text.strip()

    async def synthesize(
        self,
        text: str,
        *,
        model: str | None = None,
        voice: str | None = None,
    ) -> bytes:
        try:
            async with self._client.stream(
                "POST",
                f"{self._base_url}/audio/speech",
                json={
                    "model": model or self._tts_model,
                    "voice": voice or self._voice,
                    "input": text,
                    "response_format": "wav",
                },
            ) as response:
                response.raise_for_status()
                audio = bytearray()
                async for chunk in response.aiter_bytes():
                    audio.extend(chunk)
                    if len(audio) > self._max_audio_bytes:
                        raise ProviderError("local TTS response exceeded the audio limit")
        except httpx.HTTPError as error:
            raise ProviderError("local TTS request failed") from error
        if not audio:
            raise ProviderError("local TTS response was empty")
        return bytes(audio)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str) -> httpx.Response:
        try:
            response = await self._client.request(method, f"{self._base_url}{path}")
            response.raise_for_status()
            return response
        except httpx.HTTPError as error:
            raise ProviderError("local speech service is unavailable") from error


class HttpConsumerProvider:
    def __init__(
        self,
        *,
        endpoint: str,
        health_endpoint: str | None = None,
        token: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._endpoint = endpoint
        if health_endpoint:
            self._health_endpoint = health_endpoint
        else:
            parts = urlsplit(endpoint)
            self._health_endpoint = urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))
        self._headers = {"Authorization": f"Bearer {token}"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
        )

    async def health(self) -> None:
        try:
            response = await self._client.get(self._health_endpoint)
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ProviderError("consumer service is unavailable") from error

    async def respond(self, turn: FinalizedTurn) -> AsyncIterator[str]:
        try:
            async with self._client.stream(
                "POST",
                self._endpoint,
                headers=self._headers,
                json={
                    "session_id": turn.session_id,
                    "turn_id": turn.turn_id,
                    "generation_id": turn.generation_id,
                    "text": turn.text,
                },
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type != "application/x-ndjson":
                    raise ProviderError("consumer response was not NDJSON")
                saw_done = False
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = _parse_consumer_event(line, generation_id=turn.generation_id)
                    if event[0] == "text.delta":
                        yield event[1]
                    else:
                        saw_done = True
                        break
                if not saw_done:
                    raise ProviderError("consumer response ended without text.done")
        except httpx.HTTPError as error:
            raise ProviderError("consumer response request failed") from error

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _parse_consumer_event(line: str, *, generation_id: str) -> tuple[str, str]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as error:
        raise ProviderError("consumer response contained invalid JSON") from error
    if not isinstance(payload, dict) or payload.get("generation_id") != generation_id:
        raise ProviderError("consumer response generation did not match")
    event_type = payload.get("type")
    if event_type == "text.delta" and isinstance(payload.get("text"), str):
        return event_type, payload["text"]
    if event_type == "text.done":
        return event_type, ""
    raise ProviderError("consumer response contained an unsupported event")
