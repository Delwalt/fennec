from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import logging
from pathlib import Path
import secrets

from aiortc import RTCConfiguration, RTCIceServer, RTCSessionDescription
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .auth import SessionTokenError, issue_session_token, issue_turn_credential, verify_session_token
from .config import Settings, Tenant
from .conversation import ConversationRuntime
from .providers import HttpConsumerProvider, LocalSpeechProvider, ProviderError
from .session import SessionCapacityError, SessionRegistry
from .session_configuration import VoiceConfiguration, VoiceConfigurationOverrides

# uvicorn is started with --no-access-log and never configures the root logger,
# so every logger.info() in this app (including per-session telemetry) was
# silently dropped below the Python default of WARNING.
logging.basicConfig(level=logging.INFO)


class SessionCreateRequest(BaseModel):
    client_label: str | None = Field(default=None, max_length=128)
    configuration: VoiceConfigurationOverrides | None = None


class DevConfigurationResponse(BaseModel):
    defaults: VoiceConfiguration


class IceServer(BaseModel):
    urls: str
    username: str
    credential: str


class ClientSession(BaseModel):
    session_id: str
    signaling_url: str
    candidates_url: str
    access_token: str
    expires_at: datetime
    ice_servers: list[IceServer] = Field(default_factory=list)


class SessionDescription(BaseModel):
    sdp: str = Field(min_length=1)
    type: str = Field(pattern="^(offer|answer)$")


class TrickleCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate: str = Field(min_length=1)
    sdp_mid: str | None = Field(default=None, alias="sdpMid")
    sdp_mline_index: int | None = Field(default=None, alias="sdpMLineIndex")


def _bearer_token(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token required",
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="bearer token required",
        )
    return token


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()

    def issue_ice_servers(session_id: str) -> tuple[RTCConfiguration | None, list[IceServer]]:
        """Mint a fresh, session-scoped TURN credential rather than share one
        static long-lived secret between the gateway and every browser."""
        if not resolved.turn_url:
            return None, []
        credential = issue_turn_credential(
            session_id=session_id,
            shared_secret=resolved.turn_shared_secret,
            ttl_seconds=resolved.turn_credential_ttl_seconds,
        )
        rtc_configuration = RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=resolved.turn_url,
                    username=credential.username,
                    credential=credential.password,
                )
            ]
        )
        ice_servers = [
            IceServer(
                urls=resolved.turn_public_url,
                username=credential.username,
                credential=credential.password,
            )
        ]
        return rtc_configuration, ice_servers

    default_voice_configuration = VoiceConfiguration.from_settings(resolved)
    conversation_runtime: ConversationRuntime | None = None
    if resolved.conversation_enabled:
        conversation_runtime = ConversationRuntime(
            speech=LocalSpeechProvider(
                base_url=resolved.speech_base_url,
                stt_model=resolved.stt_model,
                tts_model=resolved.tts_model,
                voice=resolved.tts_voice,
                language=resolved.speech_language,
                timeout_seconds=resolved.provider_timeout_seconds,
            ),
            consumers={
                tenant.id: HttpConsumerProvider(
                    endpoint=tenant.consumer_url,
                    health_endpoint=tenant.consumer_health_url or None,
                    token=tenant.consumer_token,
                    timeout_seconds=resolved.provider_timeout_seconds,
                )
                for tenant in resolved.resolved_tenants
            },
            default_configuration=default_voice_configuration,
        )
    registry = SessionRegistry(
        max_sessions=resolved.max_sessions,
        conversation_runtime=conversation_runtime,
    )
    static_dir = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if conversation_runtime is not None:
            conversation_runtime.start()
        yield
        await registry.close_all()
        if conversation_runtime is not None:
            await conversation_runtime.close()

    app = FastAPI(
        title="Fennec Gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.registry = registry
    app.state.conversation_runtime = conversation_runtime

    async def require_tenant(authorization: str | None = Header(default=None)) -> Tenant:
        """The service token is the tenant identity - it decides which backend
        the session's turns are delivered to, so it is never taken from anything
        the caller merely asserts (a client label, a header, a body field)."""
        token = _bearer_token(authorization)
        for tenant in resolved.resolved_tenants:
            if hmac.compare_digest(token, tenant.service_token):
                return tenant
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid service credential")

    def create_client_session(tenant: Tenant) -> tuple[ClientSession, RTCConfiguration | None]:
        session_id = secrets.token_urlsafe(18)
        # The browser posts its offer to this URL from the tenant's own page, and the
        # gateway sends no CORS headers, so the origin has to be the tenant's own.
        base_url = tenant.public_base_url or resolved.public_base_url
        issued = issue_session_token(
            session_id=session_id,
            secret=resolved.session_secret,
            ttl_seconds=resolved.session_ttl_seconds,
        )
        rtc_configuration, ice_servers = issue_ice_servers(session_id)
        client_session = ClientSession(
            session_id=session_id,
            signaling_url=f"{base_url}/v1/sessions/{session_id}/offer",
            candidates_url=f"{base_url}/v1/sessions/{session_id}/candidates",
            access_token=issued.value,
            expires_at=issued.expires_at,
            ice_servers=ice_servers,
        )
        return client_session, rtc_configuration

    async def register_client_session(
        tenant: Tenant,
        configuration: VoiceConfigurationOverrides | None = None,
    ) -> ClientSession:
        if conversation_runtime is not None and not conversation_runtime.ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="conversation services are not ready",
            )
        voice_configuration = (
            configuration.resolve(default_voice_configuration)
            if configuration is not None
            else default_voice_configuration
        )
        if conversation_runtime is not None:
            try:
                await conversation_runtime.prepare_configuration(voice_configuration)
            except ProviderError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="requested speech models could not be prepared",
                ) from error
        client_session, rtc_configuration = create_client_session(tenant)
        try:
            await registry.create(
                session_id=client_session.session_id,
                expires_at=client_session.expires_at,
                tenant_id=tenant.id,
                configuration=voice_configuration,
                rtc_configuration=rtc_configuration,
            )
        except SessionCapacityError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="voice session capacity reached",
            ) from error
        return client_session

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        if conversation_runtime is not None and not conversation_runtime.ready:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": conversation_runtime.status,
                    "transport": "webrtc",
                    "conversation": "not_ready",
                },
            )
        return JSONResponse(
            content={
                "status": "ready",
                "transport": "webrtc",
                "conversation": "ready" if conversation_runtime is not None else "disabled",
            }
        )

    @app.post(
        "/v1/sessions",
        response_model=ClientSession,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        request: SessionCreateRequest,
        tenant: Tenant = Depends(require_tenant),
    ) -> ClientSession:
        return await register_client_session(tenant, request.configuration)

    @app.post("/dev/sessions", response_model=ClientSession, status_code=status.HTTP_201_CREATED)
    async def create_dev_session(
        request: SessionCreateRequest | None = None,
    ) -> ClientSession:
        if not resolved.dev_mode:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return await register_client_session(
            resolved.resolved_tenants[0],
            request.configuration if request else None,
        )

    @app.post("/v1/sessions/{session_id}/offer", response_model=SessionDescription)
    async def accept_offer(
        session_id: str,
        description: SessionDescription,
        authorization: str | None = Header(default=None),
    ) -> SessionDescription:
        if description.type != "offer":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="an SDP offer is required")
        token = _bearer_token(authorization)
        try:
            verify_session_token(token=token, session_id=session_id, secret=resolved.session_secret)
        except SessionTokenError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

        session = await registry.get(session_id, now=datetime.now(timezone.utc))
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found or expired")
        try:
            answer = await session.accept_offer(
                RTCSessionDescription(sdp=description.sdp, type=description.type)
            )
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid SDP offer") from error
        return SessionDescription(sdp=answer.sdp, type=answer.type)

    @app.post("/v1/sessions/{session_id}/candidates", status_code=status.HTTP_204_NO_CONTENT)
    async def add_candidate(
        session_id: str,
        candidate: TrickleCandidate,
        authorization: str | None = Header(default=None),
    ) -> Response:
        token = _bearer_token(authorization)
        try:
            verify_session_token(token=token, session_id=session_id, secret=resolved.session_secret)
        except SessionTokenError as error:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(error)) from error

        session = await registry.get(session_id, now=datetime.now(timezone.utc))
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found or expired")
        try:
            await session.add_ice_candidate(
                candidate.candidate,
                sdp_mid=candidate.sdp_mid,
                sdp_mline_index=candidate.sdp_mline_index,
            )
        except RuntimeError as error:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid ICE candidate") from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    if resolved.dev_mode:
        app.mount("/dev/assets", StaticFiles(directory=static_dir), name="dev-assets")

        @app.get("/dev/configuration", response_model=DevConfigurationResponse)
        async def dev_configuration() -> DevConfigurationResponse:
            return DevConfigurationResponse(defaults=default_voice_configuration)

        @app.get("/dev", include_in_schema=False)
        async def dev_harness() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse(url="/dev")
    else:
        @app.get("/", include_in_schema=False)
        async def root(request: Request) -> dict[str, str]:
            return {"service": "fennec-gateway", "request_id": request.headers.get("x-request-id", "")}

    return app


app = create_app()
