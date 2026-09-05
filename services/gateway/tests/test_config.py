import pytest

from fennec_gateway.config import ConfigurationError, Settings, Tenant, _read_tenants


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


def test_flat_consumer_settings_become_the_single_implicit_tenant() -> None:
    settings = Settings(
        service_token="x" * 24,
        session_secret="x" * 32,
        conversation_enabled=True,
        consumer_url="http://backend.test/v1/turns",
        consumer_token="y" * 24,
    )

    assert [tenant.id for tenant in settings.resolved_tenants] == ["default"]
    assert settings.resolved_tenants[0].consumer_url == "http://backend.test/v1/turns"


def test_configured_tenants_replace_the_flat_service_and_consumer_credentials() -> None:
    settings = Settings(
        service_token="",
        session_secret="x" * 32,
        conversation_enabled=True,
        tenants=(
            Tenant(
                id="dex",
                service_token="d" * 24,
                consumer_url="http://dex.internal/v1/turns",
                consumer_token="D" * 24,
            ),
            Tenant(
                id="teamx",
                service_token="t" * 24,
                consumer_url="http://teamx.internal/v1/turns",
                consumer_token="T" * 24,
            ),
        ),
    )

    assert [tenant.id for tenant in settings.resolved_tenants] == ["dex", "teamx"]


def test_tenants_must_be_distinguishable_and_carry_their_own_consumer() -> None:
    strong = "d" * 24
    with pytest.raises(ConfigurationError, match="duplicate"):
        Settings(
            service_token="",
            session_secret="x" * 32,
            tenants=(
                Tenant(id="dex", service_token=strong),
                Tenant(id="dex", service_token="t" * 24),
            ),
        )

    with pytest.raises(ConfigurationError, match="service tokens must be unique"):
        Settings(
            service_token="",
            session_secret="x" * 32,
            tenants=(
                Tenant(id="dex", service_token=strong),
                Tenant(id="teamx", service_token=strong),
            ),
        )

    with pytest.raises(ConfigurationError, match="at least 24 characters"):
        Settings(
            service_token="",
            session_secret="x" * 32,
            tenants=(Tenant(id="dex", service_token="short"),),
        )

    with pytest.raises(ConfigurationError, match="tenant 'dex' consumer_url"):
        Settings(
            service_token="",
            session_secret="x" * 32,
            conversation_enabled=True,
            tenants=(Tenant(id="dex", service_token=strong, consumer_token="D" * 24),),
        )


def test_a_tenant_may_carry_its_own_browser_origin() -> None:
    settings = Settings(
        service_token="",
        session_secret="x" * 32,
        public_base_url="https://gateway.test",
        conversation_enabled=True,
        tenants=(
            Tenant(
                id="dex",
                service_token="d" * 24,
                consumer_url="http://dex.internal/v1/turns",
                consumer_token="D" * 24,
                public_base_url="https://dex.test/fennec",
            ),
            Tenant(
                id="teamx",
                service_token="t" * 24,
                consumer_url="http://teamx.internal/v1/turns",
                consumer_token="T" * 24,
            ),
        ),
    )

    assert settings.resolved_tenants[0].public_base_url == "https://dex.test/fennec"
    # Unset means the gateway-wide origin, which is what one-hostname deployments want.
    assert settings.resolved_tenants[1].public_base_url == ""

    with pytest.raises(ConfigurationError, match="tenant 'dex' public_base_url"):
        Settings(
            service_token="",
            session_secret="x" * 32,
            tenants=(Tenant(id="dex", service_token="d" * 24, public_base_url="dex.test"),),
        )


def test_tenant_json_is_parsed_strictly() -> None:
    assert _read_tenants(None) == ()
    assert _read_tenants("   ") == ()
    assert _read_tenants(
        '[{"id": "dex", "service_token": "d", "consumer_url": "http://dex.internal/v1/turns"}]'
    ) == (Tenant(id="dex", service_token="d", consumer_url="http://dex.internal/v1/turns"),)

    with pytest.raises(ConfigurationError, match="valid JSON"):
        _read_tenants("not json")

    with pytest.raises(ConfigurationError, match="non-empty JSON array"):
        _read_tenants("[]")

    with pytest.raises(ConfigurationError, match="needs id and service_token"):
        _read_tenants('[{"id": "dex"}]')

    assert _read_tenants(
        '[{"id": "dex", "service_token": "d", "public_base_url": "https://dex.test/fennec/"}]'
    ) == (Tenant(id="dex", service_token="d", public_base_url="https://dex.test/fennec"),)

    with pytest.raises(ConfigurationError, match="unknown FENNEC_TENANTS keys: callback_url"):
        _read_tenants('[{"id": "dex", "service_token": "d", "callback_url": "http://evil.test"}]')
