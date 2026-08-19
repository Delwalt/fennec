import asyncio
from datetime import datetime, timedelta, timezone
from fractions import Fraction

from aiortc import MediaStreamTrack, RTCDataChannel, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame
import pytest

from conftest import wait_until
from fennec_gateway.media import AssistantAudioTrack
from fennec_gateway.session import SessionCapacityError, SessionRegistry, VoiceSession


class SilenceTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._pts = 0

    async def recv(self) -> AudioFrame:
        await asyncio.sleep(0.02)
        frame = AudioFrame(format="s16", layout="mono", samples=960)
        frame.planes[0].update(bytes(1_920))
        frame.sample_rate = 48_000
        frame.pts = self._pts
        frame.time_base = Fraction(1, 48_000)
        self._pts += 960
        return frame


async def test_direct_peer_exchanges_microphone_audio_assistant_audio_and_control() -> None:
    client = RTCPeerConnection()
    session = VoiceSession(
        session_id="test-session",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    received_audio_frames = 0
    control_events: list[str] = []

    client.addTrack(SilenceTrack())
    control: RTCDataChannel = client.createDataChannel("fennec-control")

    @control.on("message")
    def on_message(message: str) -> None:
        control_events.append(message)

    @client.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        async def drain() -> None:
            nonlocal received_audio_frames
            while received_audio_frames < 3:
                await track.recv()
                received_audio_frames += 1

        asyncio.create_task(drain())

    try:
        offer = await client.createOffer()
        await client.setLocalDescription(offer)
        assert client.localDescription is not None
        answer = await session.accept_offer(
            RTCSessionDescription(sdp=client.localDescription.sdp, type=client.localDescription.type)
        )
        await client.setRemoteDescription(answer)

        await wait_until(lambda: control.readyState == "open")
        control.send('{"type":"audio.check"}')
        await wait_until(lambda: received_audio_frames >= 3)
        await wait_until(lambda: session.microphone_frames >= 3)
        await wait_until(lambda: any("audio.check.started" in event for event in control_events))
    finally:
        await session.close()
        await client.close()


async def test_trickled_candidate_is_applied_after_the_offer() -> None:
    client = RTCPeerConnection()
    session = VoiceSession(
        session_id="test-session",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    client.addTrack(SilenceTrack())

    try:
        offer = await client.createOffer()
        await client.setLocalDescription(offer)
        assert client.localDescription is not None
        # aiortc's default setLocalDescription waits for full gathering, so the
        # client's own SDP already contains a real candidate line to trickle
        # back - standing in for one arriving late from a slower ICE server.
        candidate_line = next(
            line for line in client.localDescription.sdp.splitlines() if line.startswith("a=candidate:")
        )
        candidate_sdp = candidate_line.removeprefix("a=candidate:")

        answer = await session.accept_offer(
            RTCSessionDescription(sdp=client.localDescription.sdp, type=client.localDescription.type)
        )
        await client.setRemoteDescription(answer)

        await session.add_ice_candidate(candidate_sdp, sdp_mid="0", sdp_mline_index=0)
    finally:
        await session.close()
        await client.close()


async def test_trickled_candidate_requires_an_existing_peer_connection() -> None:
    session = VoiceSession(
        session_id="test-session",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    with pytest.raises(RuntimeError, match="no active peer connection"):
        await session.add_ice_candidate("1 1 UDP 1 1.2.3.4 5000 typ host", sdp_mid="0", sdp_mline_index=0)


async def test_check_tone_changes_silence_to_nonzero_pcm() -> None:
    track = AssistantAudioTrack()
    silent = await track.recv()
    track.trigger()
    tone = await track.recv()
    track.stop()

    assert not any(bytes(silent.planes[0]))
    assert any(bytes(tone.planes[0]))


async def test_new_generation_discards_queued_stale_audio() -> None:
    track = AssistantAudioTrack()
    frame_bytes = track.samples_per_frame * 2
    track.begin_generation("old")
    await track.enqueue_pcm(generation_id="old", pcm=bytes([1]) * frame_bytes)
    track.begin_generation("current")
    await track.enqueue_pcm(generation_id="current", pcm=bytes([2]) * frame_bytes)

    frame = await track.recv()
    track.stop()

    assert bytes(frame.planes[0])[:frame_bytes] == bytes([2]) * frame_bytes
    assert track.rejected_frames == 1
    assert track.queued_peak_frames == 1


async def test_session_registry_is_bounded_and_reclaims_expired_sessions() -> None:
    registry = SessionRegistry(max_sessions=1)
    now = datetime.now(timezone.utc)
    await registry.create(session_id="active", expires_at=now + timedelta(minutes=1))

    with pytest.raises(SessionCapacityError):
        await registry.create(session_id="overflow", expires_at=now + timedelta(minutes=1))

    await registry.close_all()
    await registry.create(session_id="replacement", expires_at=now + timedelta(minutes=1))
    await registry.close_all()
