from datetime import datetime, timezone

from httpx import ASGITransport, AsyncClient

from fennec_gateway.app import create_app
from fennec_gateway.auth import issue_session_token
from fennec_gateway.config import Settings, Tenant


SERVICE_TOKEN = "test-service-token-at-least-24-characters"
SETTINGS = Settings(
    service_token=SERVICE_TOKEN,
    session_secret="test-session-secret-at-least-32-characters-long",
    public_base_url="http://gateway.test",
    dev_mode=False,
)


def client_for(settings: Settings = SETTINGS) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


async def test_health_checks_have_distinct_contracts() -> None:
    async with client_for() as client:
        assert (await client.get("/health/live")).json() == {"status": "live"}
        assert (await client.get("/health/ready")).json() == {
            "status": "ready",
            "transport": "webrtc",
            "conversation": "disabled",
        }


async def test_session_creation_requires_backend_credential() -> None:
    async with client_for() as client:
        missing = await client.post("/v1/sessions", json={})
        wrong = await client.post(
            "/v1/sessions",
            headers={"Authorization": "Bearer definitely-not-the-service-token"},
            json={},
        )

    assert missing.status_code == 401
    assert wrong.status_code == 403


async def test_session_creation_returns_only_browser_safe_fields() -> None:
    async with client_for() as client:
        response = await client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={"client_label": "TeamX"},
        )

    assert response.status_code == 201
    payload = response.json()
    assert set(payload) == {
        "session_id",
        "signaling_url",
        "candidates_url",
        "access_token",
        "expires_at",
        "ice_servers",
    }
    assert payload["ice_servers"] == []
    assert payload["signaling_url"] == (
        f"http://gateway.test/v1/sessions/{payload['session_id']}/offer"
    )
    assert SERVICE_TOKEN not in response.text


async def test_dev_session_route_is_not_available_outside_dev_mode() -> None:
    async with client_for() as client:
        response = await client.post("/dev/sessions")

    assert response.status_code == 404


async def test_dev_ui_reads_server_defaults_and_applies_bounded_session_overrides() -> None:
    settings = Settings(
        service_token=SERVICE_TOKEN,
        session_secret="test-session-secret-at-least-32-characters-long",
        public_base_url="http://gateway.test",
        dev_mode=True,
        endpoint_silence_ms=1_400,
        tts_voice="af_sky",
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        defaults = await client.get("/dev/configuration")
        created = await client.post(
            "/dev/sessions",
            json={
                "configuration": {
                    "endpoint_silence_ms": 1_800,
                    "vad_threshold": 0.4,
                }
            },
        )

    assert defaults.status_code == 200
    assert defaults.json()["defaults"]["endpoint_silence_ms"] == 1_400
    assert defaults.json()["defaults"]["tts_voice"] == "af_sky"
    assert created.status_code == 201
    session = await app.state.registry.get(
        created.json()["session_id"],
        now=datetime.now(timezone.utc),
    )
    assert session is not None
    assert session.configuration.endpoint_silence_ms == 1_800
    assert session.configuration.vad_threshold == 0.4
    assert session.configuration.tts_voice == "af_sky"
    await app.state.registry.close_all()


async def test_session_configuration_rejects_unsafe_values_and_unknown_fields() -> None:
    settings = Settings(
        service_token=SERVICE_TOKEN,
        session_secret="test-session-secret-at-least-32-characters-long",
        dev_mode=True,
    )
    async with client_for(settings) as client:
        outside_range = await client.post(
            "/dev/sessions",
            json={"configuration": {"endpoint_silence_ms": 50}},
        )
        unknown = await client.post(
            "/dev/sessions",
            json={"configuration": {"consumer_url": "https://attacker.test"}},
        )

    assert outside_range.status_code == 422
    assert unknown.status_code == 422


async def test_trickle_candidate_endpoint_requires_a_valid_session_token() -> None:
    async with client_for() as client:
        created = await client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={},
        )
        session_id = created.json()["session_id"]

        missing_auth = await client.post(
            f"/v1/sessions/{session_id}/candidates",
            json={"candidate": "1 1 UDP 1 1.2.3.4 5000 typ host"},
        )
        wrong_token = await client.post(
            f"/v1/sessions/{session_id}/candidates",
            headers={"Authorization": "Bearer definitely-wrong"},
            json={"candidate": "1 1 UDP 1 1.2.3.4 5000 typ host"},
        )
        # A validly-signed token for a session that was never created (or
        # already expired) - distinct from an outright wrong/forged token.
        phantom_token = issue_session_token(
            session_id="does-not-exist",
            secret=SETTINGS.session_secret,
            ttl_seconds=120,
        )
        unknown_session = await client.post(
            "/v1/sessions/does-not-exist/candidates",
            headers={"Authorization": f"Bearer {phantom_token.value}"},
            json={"candidate": "1 1 UDP 1 1.2.3.4 5000 typ host"},
        )

    assert missing_auth.status_code == 401
    assert wrong_token.status_code == 403
    assert unknown_session.status_code == 404


async def test_turn_configuration_issues_a_fresh_credential_per_session() -> None:
    settings = Settings(
        service_token=SERVICE_TOKEN,
        session_secret="test-session-secret-at-least-32-characters-long",
        dev_mode=True,
        turn_url="turn:turn.internal:3478?transport=udp",
        turn_public_url="turn:turn.example:3478?transport=udp",
        turn_shared_secret="x" * 32,
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post("/dev/sessions")
        second = await client.post("/dev/sessions")

    first_payload = first.json()
    second_payload = second.json()

    # The server's own peer connection uses the internal TURN URL...
    session = await app.state.registry.get(
        first_payload["session_id"],
        now=datetime.now(timezone.utc),
    )
    assert session is not None
    assert session.rtc_configuration is not None
    assert session.rtc_configuration.iceServers[0].urls == settings.turn_url

    # ...while the browser only ever receives the public URL, and each
    # session gets its own credential rather than one shared static secret.
    assert len(first_payload["ice_servers"]) == 1
    ice_server = first_payload["ice_servers"][0]
    assert ice_server["urls"] == settings.turn_public_url
    assert session.rtc_configuration.iceServers[0].username == ice_server["username"]
    assert session.rtc_configuration.iceServers[0].credential == ice_server["credential"]
    assert settings.turn_shared_secret not in first.text
    assert first_payload["ice_servers"][0]["username"] != second_payload["ice_servers"][0]["username"]

    await app.state.registry.close_all()


async def test_conversation_profile_refuses_sessions_until_dependencies_are_ready() -> None:
    settings = Settings(
        service_token=SERVICE_TOKEN,
        session_secret="test-session-secret-at-least-32-characters-long",
        public_base_url="http://gateway.test",
        conversation_enabled=True,
        consumer_token="test-consumer-token-at-least-24-characters",
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        readiness = await client.get("/health/ready")
        session = await client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={},
        )
    await app.state.conversation_runtime.close()

    assert readiness.status_code == 503
    assert readiness.json()["status"] == "warming"
    assert session.status_code == 503


DEX_TOKEN = "dex-service-token-at-least-24-characters"
TEAMX_TOKEN = "teamx-service-token-at-least-24-characters"
MULTI_TENANT_SETTINGS = Settings(
    service_token="",
    session_secret="test-session-secret-at-least-32-characters-long",
    public_base_url="http://gateway.test",
    tenants=(
        Tenant(id="dex", service_token=DEX_TOKEN, consumer_url="http://dex.internal/v1/turns"),
        Tenant(id="teamx", service_token=TEAMX_TOKEN, consumer_url="http://teamx.internal/v1/turns"),
    ),
)


async def test_each_tenant_opens_sessions_with_its_own_service_token() -> None:
    app = create_app(MULTI_TENANT_SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        dex = await client.post("/v1/sessions", headers={"Authorization": f"Bearer {DEX_TOKEN}"}, json={})
        teamx = await client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {TEAMX_TOKEN}"},
            json={},
        )
        stranger = await client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {SERVICE_TOKEN}"},
            json={},
        )

    assert dex.status_code == 201
    assert teamx.status_code == 201
    assert stranger.status_code == 403

    registry = app.state.registry
    now = datetime.now(timezone.utc)
    assert (await registry.get(dex.json()["session_id"], now=now)).tenant_id == "dex"
    assert (await registry.get(teamx.json()["session_id"], now=now)).tenant_id == "teamx"


async def test_a_client_label_cannot_choose_the_tenant_it_is_delivered_to() -> None:
    # The label is descriptive only: routing follows the service token, so a browser
    # calling itself "teamx" never reaches TeamX's backend.
    app = create_app(MULTI_TENANT_SETTINGS)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/sessions",
            headers={"Authorization": f"Bearer {DEX_TOKEN}"},
            json={"client_label": "teamx"},
        )

    session = await app.state.registry.get(
        response.json()["session_id"], now=datetime.now(timezone.utc)
    )
    assert session.tenant_id == "dex"
