from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from fractions import Fraction
import json
from time import monotonic
from typing import Any

from aiortc import MediaStreamTrack, RTCDataChannel, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame
import httpx
import numpy as np

from .audio import decode_audio_to_pcm16_mono
from .config import Settings
from .providers import LocalSpeechProvider


class FixtureMicrophoneTrack(MediaStreamTrack):
    kind = "audio"
    sample_rate = 48_000
    samples_per_frame = 960

    def __init__(self, pcm: bytes) -> None:
        super().__init__()
        self._pcm = bytearray(pcm)
        self._offset = 0
        self._pts = 0
        self._started_at: float | None = None

    async def recv(self) -> AudioFrame:
        loop = asyncio.get_running_loop()
        if self._started_at is None:
            self._started_at = loop.time()
        delay = self._started_at + self._pts / self.sample_rate - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)

        frame_bytes = self.samples_per_frame * 2
        chunk = self._pcm[self._offset : self._offset + frame_bytes]
        self._offset += len(chunk)
        if len(chunk) < frame_bytes:
            chunk += bytes(frame_bytes - len(chunk))
        frame = AudioFrame(format="s16", layout="mono", samples=self.samples_per_frame)
        frame.planes[0].update(chunk)
        frame.sample_rate = self.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self.sample_rate)
        self._pts += self.samples_per_frame
        return frame

    def inject(self, pcm: bytes) -> None:
        self._pcm.extend(pcm)


@dataclass(slots=True)
class SmokeResult:
    transcript: str = ""
    transcripts: list[str] = field(default_factory=list)
    assistant_text: str = ""
    assistant_audio_frames: int = 0
    cancellation_latency_ms: float | None = None
    server_cancellation_latency_ms: float | None = None
    stale_audio_frames_received: int = 0
    stale_audio_frames_played: int = 0
    consumer_disconnects: int = 0
    telemetry: dict[str, Any] | None = None
    events: list[str] = field(default_factory=list)


async def run_smoke(
    prompt: str = "Fennec can hear this local voice test.",
    *,
    interruption_prompt: str | None = None,
    pause_prompt: str | None = None,
) -> SmokeResult:
    if interruption_prompt is not None and pause_prompt is not None:
        raise ValueError("interruption and pause modes are mutually exclusive")
    settings = Settings.from_env()
    speech = LocalSpeechProvider(
        base_url=settings.speech_base_url,
        stt_model=settings.stt_model,
        tts_model=settings.tts_model,
        voice=settings.tts_voice,
        language=settings.speech_language,
        timeout_seconds=settings.provider_timeout_seconds,
    )
    client = httpx.AsyncClient(timeout=settings.provider_timeout_seconds)
    peer = RTCPeerConnection()
    result = SmokeResult()
    done = asyncio.Event()
    speaking = asyncio.Event()
    drain_tasks: set[asyncio.Task[Any]] = set()

    try:
        disconnects_before = 0
        if interruption_prompt is not None:
            metrics_response = await client.get("http://127.0.0.1:8090/metrics")
            metrics_response.raise_for_status()
            disconnects_before = int(metrics_response.json()["client_disconnects"])
        fixture_wav = await speech.synthesize(prompt)
        fixture_pcm = await asyncio.to_thread(
            decode_audio_to_pcm16_mono,
            fixture_wav,
            sample_rate=48_000,
        )
        if pause_prompt is not None:
            pause_wav = await speech.synthesize(pause_prompt)
            pause_pcm = await asyncio.to_thread(
                decode_audio_to_pcm16_mono,
                pause_wav,
                sample_rate=48_000,
            )
            thinking_pause = bytes(int(48_000 * 0.85) * 2)
            fixture_pcm = fixture_pcm + thinking_pause + pause_pcm
        interruption_pcm: bytes | None = None
        if interruption_prompt is not None:
            interruption_wav = await speech.synthesize(interruption_prompt)
            interruption_pcm = await asyncio.to_thread(
                decode_audio_to_pcm16_mono,
                interruption_wav,
                sample_rate=48_000,
            )
        microphone = FixtureMicrophoneTrack(fixture_pcm)
        peer.addTrack(microphone)
        control: RTCDataChannel = peer.createDataChannel("fennec-control", ordered=True)
        summary_received = asyncio.Event()
        cancellation_received = asyncio.Event()
        speech_events = 0
        speaking_events = 0
        interruption_injected = False
        interruption_confirmed_at: float | None = None
        playback_enabled = True

        @control.on("message")
        def on_message(raw: Any) -> None:
            nonlocal speech_events, speaking_events, interruption_confirmed_at, playback_enabled
            if not isinstance(raw, str):
                return
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                return
            event_type = event.get("type")
            if not isinstance(event_type, str):
                return
            result.events.append(event_type)
            if event_type == "speech.started":
                speech_events += 1
                if interruption_injected and speech_events >= 2:
                    interruption_confirmed_at = monotonic()
            elif event_type == "transcript.final":
                result.transcript = event.get("text", "")
                result.transcripts.append(result.transcript)
            elif event_type == "assistant.text.delta":
                result.assistant_text += event.get("text", "")
            elif event_type == "assistant.speaking":
                speaking_events += 1
                playback_enabled = True
                speaking.set()
            elif event_type == "assistant.cancelled":
                playback_enabled = False
                if interruption_confirmed_at is not None:
                    result.cancellation_latency_ms = round(
                        (monotonic() - interruption_confirmed_at) * 1_000,
                        1,
                    )
                server_latency = event.get("latency_ms")
                if isinstance(server_latency, (int, float)):
                    result.server_cancellation_latency_ms = float(server_latency)
                cancellation_received.set()
            elif event_type == "assistant.done":
                if interruption_prompt is None or (
                    cancellation_received.is_set() and speaking_events >= 2
                ):
                    done.set()
            elif event_type == "telemetry.session.summary":
                result.telemetry = event
                summary_received.set()
            elif event_type == "error":
                done.set()

        @peer.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            async def drain() -> None:
                nonlocal interruption_injected
                while True:
                    frame = await track.recv()
                    non_silent = bool(np.any(frame.to_ndarray()))
                    if not speaking.is_set() or not non_silent:
                        continue
                    if cancellation_received.is_set() and speaking_events < 2:
                        result.stale_audio_frames_received += 1
                        if playback_enabled:
                            result.stale_audio_frames_played += 1
                    elif playback_enabled:
                        result.assistant_audio_frames += 1
                    if (
                        interruption_pcm is not None
                        and not interruption_injected
                        and speaking_events == 1
                    ):
                        microphone.inject(interruption_pcm)
                        interruption_injected = True

            task = asyncio.create_task(drain())
            drain_tasks.add(task)
            task.add_done_callback(drain_tasks.discard)

        session_response = await client.post(
            "http://127.0.0.1:8080/v1/sessions",
            headers={"Authorization": f"Bearer {settings.resolved_tenants[0].service_token}"},
            json={"client_label": "container-smoke"},
        )
        session_response.raise_for_status()
        session = session_response.json()

        offer = await peer.createOffer()
        await peer.setLocalDescription(offer)
        if peer.localDescription is None:
            raise RuntimeError("smoke client did not create a local offer")
        answer_response = await client.post(
            session["signaling_url"],
            headers={"Authorization": f"Bearer {session['access_token']}"},
            json={
                "type": peer.localDescription.type,
                "sdp": peer.localDescription.sdp,
            },
        )
        answer_response.raise_for_status()
        answer = answer_response.json()
        await peer.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )

        started = monotonic()
        async with asyncio.timeout(settings.provider_timeout_seconds):
            await done.wait()
            while result.assistant_audio_frames == 0 and monotonic() - started < 10:
                await asyncio.sleep(0.05)

        if "error" in result.events:
            raise RuntimeError("gateway reported an error during the conversation smoke")
        if not result.transcript:
            raise RuntimeError("conversation smoke did not receive a final transcript")
        if not result.assistant_text:
            raise RuntimeError("conversation smoke did not receive assistant text")
        if result.assistant_audio_frames == 0:
            raise RuntimeError("conversation smoke did not receive non-silent assistant audio")
        if pause_prompt is not None:
            if len(result.transcripts) != 1:
                raise RuntimeError("pause smoke split or discarded the continued utterance")
            if result.events.count("turn.committed") != 1:
                raise RuntimeError("pause smoke committed more than one user turn")
            if "assistant.cancelled" in result.events:
                raise RuntimeError("pause smoke incorrectly interrupted the assistant")
        if interruption_prompt is not None:
            if len(result.transcripts) != 2:
                raise RuntimeError("barge-in smoke did not produce exactly two final transcripts")
            if result.cancellation_latency_ms is None:
                raise RuntimeError("barge-in smoke did not receive a confirmed cancellation")
            if result.cancellation_latency_ms >= 200:
                raise RuntimeError("barge-in cancellation exceeded 200 ms after confirmation")
            if result.stale_audio_frames_played:
                raise RuntimeError("barge-in smoke played stale assistant audio")
            metrics_response = await client.get("http://127.0.0.1:8090/metrics")
            metrics_response.raise_for_status()
            result.consumer_disconnects = (
                int(metrics_response.json()["client_disconnects"]) - disconnects_before
            )
            if result.consumer_disconnects < 1:
                raise RuntimeError("barge-in smoke did not cancel the active consumer stream")
        if control.readyState == "open":
            control.send(json.dumps({"type": "session.close"}))
            try:
                async with asyncio.timeout(2):
                    await summary_received.wait()
            except TimeoutError:
                pass
        return result
    finally:
        for task in list(drain_tasks):
            task.cancel()
        if drain_tasks:
            await asyncio.gather(*drain_tasks, return_exceptions=True)
        await peer.close()
        await client.aclose()
        await speech.close()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the local Fennec conversation loop")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--barge-in", action="store_true")
    mode.add_argument("--pause", action="store_true")
    options = parser.parse_args()
    result = await run_smoke(
        interruption_prompt=(
            "Stop there and tell me the short answer instead."
            if options.barge_in
            else None
        ),
        pause_prompt=(
            "This sentence follows after a natural thinking pause."
            if options.pause
            else None
        ),
    )
    mode_name = (
        "barge-in" if options.barge_in else "pause" if options.pause else "conversation"
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "mode": mode_name,
                "transcript": result.transcript,
                "transcripts": result.transcripts,
                "assistant_text": result.assistant_text,
                "assistant_audio_frames": result.assistant_audio_frames,
                "cancellation_latency_ms": result.cancellation_latency_ms,
                "server_cancellation_latency_ms": result.server_cancellation_latency_ms,
                "stale_audio_frames_received": result.stale_audio_frames_received,
                "stale_audio_frames_played": result.stale_audio_frames_played,
                "consumer_disconnects": result.consumer_disconnects,
                "telemetry": result.telemetry,
                "events": result.events,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
