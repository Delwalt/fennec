from datetime import datetime, timedelta, timezone

import jwt
import pytest

from fennec_gateway.auth import (
    ALGORITHM,
    AUDIENCE,
    ISSUER,
    SessionTokenError,
    issue_session_token,
    verify_session_token,
)


SECRET = "test-session-secret-at-least-32-characters-long"


def test_session_token_is_scoped_to_one_session() -> None:
    issued = issue_session_token(session_id="session-a", secret=SECRET, ttl_seconds=60)

    verify_session_token(token=issued.value, session_id="session-a", secret=SECRET)

    with pytest.raises(SessionTokenError, match="does not match"):
        verify_session_token(token=issued.value, session_id="session-b", secret=SECRET)


def test_expired_session_token_is_rejected() -> None:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": "session-a",
            "iat": now - timedelta(minutes=2),
            "nbf": now - timedelta(minutes=2),
            "exp": now - timedelta(minutes=1),
            "jti": "expired",
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(SessionTokenError, match="invalid or expired"):
        verify_session_token(token=token, session_id="session-a", secret=SECRET)
