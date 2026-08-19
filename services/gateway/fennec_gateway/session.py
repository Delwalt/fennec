from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
from typing import Any

from aiortc import RTCConfiguration, RTCDataChannel, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from aiortc.sdp import candidate_from_sdp
from av import AudioResampler

from .conversation import AudioBackpressureError, ConversationRuntime, ConversationSession
from .media import AssistantAudioTrack
from .session_configuration import VoiceConfiguration


logger = logging.getLogger("fennec.gateway")


def _event(event_type: str, **data: Any) -> str:
    return json.dumps({"type": event_type, **data}, separators=(",", ":"))


class SessionCapacityError(RuntimeError):
    """Raised when the bounded in-memory session registry is full."""


@dataclass(slots=True)
class VoiceSession:
    session_id: str
    expires_at: datetime
    conversation_runtime: ConversationRuntime | None = None
    configuration: VoiceConfiguration | None = None
    rtc_configuration: RTCConfiguration | None = None
    peer_connection: RTCPeerConnection | None = None
    audio_track: AssistantAudioTrack | None = None
    conversation: ConversationSession | None = None
    control_channel: RTCDataChannel | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    microphone_frames: int = 0
    closed: bool = False

    async def accept_offer(self, offer: RTCSessionDescription) -> RTCSessionDescription:
        if self.closed or self.peer_connection is not None:
            raise RuntimeError("session has already been used")

        peer = RTCPeerConnection(configuration=self.rtc_configuration)
        output = AssistantAudioTrack()
        self.peer_connection = peer
        self.audio_track = output
        if self.conversation_runtime is not None:
            self.conversation = self.conversation_runtime.create_session(
                session_id=self.session_id,
                output=output,
                send_event=self._send_conversation_event,
                configuration=self.configuration,
            )
        peer.addTrack(output)

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            logger.info(
                "peer state changed session_id=%s state=%s",
                self.session_id,
                peer.connectionState,
            )
            if peer.connectionState in {"disconnected", "failed", "closed"}:
                await self.close()

        @peer.on("track")
        def on_track(track: Any) -> None:
            if track.kind != "audio":
                return
            task = asyncio.create_task(self._drain_microphone(track))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

        @peer.on("datachannel")
        def on_datachannel(channel: RTCDataChannel) -> None:
            self._attach_control_channel(channel)

        try:
            await peer.setRemoteDescription(offer)
            answer = await peer.createAnswer()
            await peer.setLocalDescription(answer)
            if peer.localDescription is None:
                raise RuntimeError("WebRTC answer was not created")
            return peer.localDescription
        except Exception:
            await self.close()
            raise

    async def add_ice_candidate(
        self,
        candidate_sdp: str,
        *,
        sdp_mid: str | None,
        sdp_mline_index: int | None,
    ) -> None:
        """Apply one browser-trickled ICE candidate discovered after the offer.

        The browser no longer waits for full candidate gathering before
        sending its offer (that wait was the dominant cost in "Connecting
        voice..." taking several seconds) - it sends the offer immediately
        and trickles each candidate here as it's discovered instead.
        """
        if self.closed or self.peer_connection is None:
            raise RuntimeError("session has no active peer connection")
        candidate = candidate_from_sdp(candidate_sdp)
        candidate.sdpMid = sdp_mid
        candidate.sdpMLineIndex = sdp_mline_index
        await self.peer_connection.addIceCandidate(candidate)

    def _attach_control_channel(self, channel: RTCDataChannel) -> None:
        if channel.label != "fennec-control":
            channel.close()
            return
        self.control_channel = channel

        @channel.on("open")
        def on_open() -> None:
            channel.send(
                _event(
                    "session.ready",
                    session_id=self.session_id,
                    conversation=self.conversation is not None,
                )
            )
            if self.conversation is not None:
                channel.send(_event("state.changed", state="listening"))

        @channel.on("message")
        def on_message(message: Any) -> None:
            if not isinstance(message, str):
                channel.send(_event("error", code="unsupported_message"))
                return
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                channel.send(_event("error", code="invalid_json"))
                return

            message_type = payload.get("type")
            if message_type == "audio.check":
                if self.audio_track is not None:
                    self.audio_track.trigger()
                channel.send(_event("audio.check.started"))
            elif message_type == "ping":
                channel.send(_event("pong"))
            elif message_type == "session.close":
                task = asyncio.create_task(self.close())
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            else:
                channel.send(_event("error", code="unknown_message_type"))

    async def _drain_microphone(self, track: Any) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=16_000)
        try:
            while not self.closed:
                frame = await track.recv()
                self.microphone_frames += 1
                if self.conversation is not None:
                    for converted in resampler.resample(frame):
                        pcm = bytes(converted.planes[0])[: converted.samples * 2]
                        self.conversation.feed_audio(pcm)
        except (MediaStreamError, asyncio.CancelledError):
            pass
        except AudioBackpressureError:
            logger.warning("audio backpressure session_id=%s", self.session_id)
            await self.close()
        except Exception:
            logger.exception("microphone track failed session_id=%s", self.session_id)

    def _send_conversation_event(self, event_type: str, data: dict[str, Any]) -> None:
        channel = self.control_channel
        if channel is not None and channel.readyState == "open":
            channel.send(_event(event_type, **data))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        pending = [task for task in self.tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.tasks.clear()
        if self.conversation is not None:
            await self.conversation.close()
        if self.audio_track is not None:
            self.audio_track.stop()
        if self.peer_connection is not None:
            await self.peer_connection.close()
        logger.info(
            "session closed session_id=%s microphone_frames=%d",
            self.session_id,
            self.microphone_frames,
        )


class SessionRegistry:
    def __init__(
        self,
        *,
        max_sessions: int = 64,
        conversation_runtime: ConversationRuntime | None = None,
    ) -> None:
        self._sessions: dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()
        self._max_sessions = max_sessions
        self._conversation_runtime = conversation_runtime

    async def create(
        self,
        *,
        session_id: str,
        expires_at: datetime,
        configuration: VoiceConfiguration | None = None,
        rtc_configuration: RTCConfiguration | None = None,
    ) -> VoiceSession:
        session = VoiceSession(
            session_id=session_id,
            expires_at=expires_at,
            conversation_runtime=self._conversation_runtime,
            configuration=configuration,
            rtc_configuration=rtc_configuration,
        )
        stale: list[VoiceSession]
        at_capacity = False
        async with self._lock:
            now = datetime.now(expires_at.tzinfo)
            stale = [
                candidate
                for candidate in self._sessions.values()
                if candidate.closed or candidate.expires_at <= now
            ]
            for candidate in stale:
                self._sessions.pop(candidate.session_id, None)
            if len(self._sessions) >= self._max_sessions:
                at_capacity = True
            else:
                self._sessions[session_id] = session
        if stale:
            await asyncio.gather(*(candidate.close() for candidate in stale), return_exceptions=True)
        if at_capacity:
            raise SessionCapacityError("session capacity reached")
        return session

    async def get(self, session_id: str, *, now: datetime) -> VoiceSession | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.closed or session.expires_at <= now:
                self._sessions.pop(session_id, None)
            else:
                return session
        await session.close()
        return None

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
