import asyncio
from contextlib import suppress
import json
from io import BytesIO
import wave

import httpx
import pytest

from fennec_gateway.providers import (
    FinalizedTurn,
    HttpConsumerProvider,
    LocalSpeechProvider,
    ProviderError,
)


def wav_fixture() -> bytes:
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(bytes(3_200))
    return output.getvalue()


async def test_local_speech_provider_uses_separate_stt_and_tts_contracts() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "hello from Whisper"})
        if request.url.path == "/v1/audio/speech":
            return httpx.Response(200, content=wav_fixture(), headers={"content-type": "audio/wav"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = LocalSpeechProvider(
        base_url="http://speech.test/v1",
        stt_model="whisper-local",
        tts_model="kokoro-local",
        voice="local-voice",
        language="en",
        timeout_seconds=10,
        client=client,
    )

    assert await provider.transcribe(bytes(3_200)) == "hello from Whisper"
    assert await provider.synthesize("Hello") == wav_fixture()
    assert [request.url.path for request in requests] == [
        "/v1/audio/transcriptions",
        "/v1/audio/speech",
    ]
    assert b"whisper-local" in requests[0].content
    assert json.loads(requests[1].content)["model"] == "kokoro-local"
    await client.aclose()


async def test_local_speech_provider_downloads_only_missing_models() -> None:
    requested: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append((request.method, request.url.raw_path.decode()))
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "whisper/model"}]})
        return httpx.Response(200, json={"status": "downloaded"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = LocalSpeechProvider(
        base_url="http://speech.test/v1",
        stt_model="whisper/model",
        tts_model="kokoro/model",
        voice="voice",
        language="en",
        timeout_seconds=10,
        client=client,
    )
    await provider.ensure_models()

    assert requested == [
        ("GET", "/v1/models"),
        ("POST", "/v1/models/kokoro%2Fmodel"),
    ]
    await client.aclose()


async def test_local_speech_provider_uses_session_model_language_and_voice() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/audio/transcriptions":
            return httpx.Response(200, json={"text": "custom transcript"})
        return httpx.Response(200, content=wav_fixture())

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = LocalSpeechProvider(
        base_url="http://speech.test/v1",
        stt_model="default-stt",
        tts_model="default-tts",
        voice="default-voice",
        language="en",
        timeout_seconds=10,
        client=client,
    )

    await provider.transcribe(bytes(3_200), model="custom-stt", language="en-IN")
    await provider.synthesize("Hello", model="custom-tts", voice="custom-voice")

    assert b'custom-stt' in requests[0].content
    assert b'en-IN' in requests[0].content
    assert json.loads(requests[1].content) == {
        "model": "custom-tts",
        "voice": "custom-voice",
        "input": "Hello",
        "response_format": "wav",
    }
    await client.aclose()


async def test_consumer_provider_validates_generation_and_ndjson_completion() -> None:
    turn = FinalizedTurn(
        session_id="session",
        turn_id="turn",
        generation_id="generation",
        text="hello",
    )
    records = [
        {"type": "text.delta", "generation_id": "generation", "text": "Hi. "},
        {"type": "text.delta", "generation_id": "generation", "text": "How are you?"},
        {"type": "text.done", "generation_id": "generation"},
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer consumer-token-at-least-24"
        assert json.loads(request.content)["text"] == "hello"
        body = "".join(json.dumps(record) + "\n" for record in records)
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "application/x-ndjson"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = HttpConsumerProvider(
        endpoint="http://consumer.test/v1/turns",
        token="consumer-token-at-least-24",
        timeout_seconds=10,
        client=client,
    )

    assert [delta async for delta in provider.respond(turn)] == ["Hi. ", "How are you?"]
    await client.aclose()


async def test_consumer_provider_uses_an_explicit_health_endpoint() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    provider = HttpConsumerProvider(
        endpoint="http://consumer.test/internal/fennec/turns",
        health_endpoint="http://consumer.test/api/health",
        token="consumer-token-at-least-24",
        timeout_seconds=10,
        client=client,
    )
    await provider.health()

    assert requests == ["http://consumer.test/api/health"]
    await client.aclose()


async def test_consumer_provider_rejects_stale_generation() -> None:
    body = json.dumps(
        {"type": "text.delta", "generation_id": "stale", "text": "late"}
    ) + "\n"
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                text=body,
                headers={"content-type": "application/x-ndjson"},
            )
        )
    )
    provider = HttpConsumerProvider(
        endpoint="http://consumer.test/v1/turns",
        token="consumer-token-at-least-24",
        timeout_seconds=10,
        client=client,
    )

    with pytest.raises(ProviderError, match="generation did not match"):
        _ = [
            delta
            async for delta in provider.respond(
                FinalizedTurn("session", "turn", "current", "hello")
            )
        ]
    await client.aclose()


class SlowConsumerStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b'{"type":"text.delta","generation_id":"generation","text":"Start. "}\n'
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed = True


async def test_consumer_provider_closes_the_http_stream_when_generation_is_cancelled() -> None:
    stream = SlowConsumerStream()
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "application/x-ndjson"},
            )
        )
    )
    provider = HttpConsumerProvider(
        endpoint="http://consumer.test/v1/turns",
        token="consumer-token-at-least-24",
        timeout_seconds=10,
        client=client,
    )
    response = provider.respond(
        FinalizedTurn("session", "turn", "generation", "hello")
    )

    assert await anext(response) == "Start. "
    pending = asyncio.create_task(anext(response))
    await asyncio.sleep(0)
    pending.cancel()
    with suppress(asyncio.CancelledError):
        await pending

    assert stream.closed is True
    await client.aclose()
