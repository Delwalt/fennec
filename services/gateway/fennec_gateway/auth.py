from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets

import jwt


ISSUER = "fennec-gateway"
AUDIENCE = "fennec-browser"
ALGORITHM = "HS256"


class SessionTokenError(ValueError):
    """Raised when a browser session token is invalid or expired."""


@dataclass(frozen=True, slots=True)
class IssuedToken:
    value: str
    expires_at: datetime


def issue_session_token(*, session_id: str, secret: str, ttl_seconds: int) -> IssuedToken:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)
    value = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": session_id,
            "iat": now,
            "nbf": now,
            "exp": expires_at,
            "jti": secrets.token_urlsafe(16),
        },
        secret,
        algorithm=ALGORITHM,
    )
    return IssuedToken(value=value, expires_at=expires_at)


def verify_session_token(*, token: str, session_id: str, secret: str) -> None:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            issuer=ISSUER,
            options={"require": ["exp", "iat", "nbf", "sub", "jti"]},
        )
    except jwt.PyJWTError as error:
        raise SessionTokenError("invalid or expired session token") from error

    if claims.get("sub") != session_id:
        raise SessionTokenError("session token does not match the requested session")


@dataclass(frozen=True, slots=True)
class TurnCredential:
    username: str
    password: str
    expires_at: datetime


def issue_turn_credential(*, session_id: str, shared_secret: str, ttl_seconds: int) -> TurnCredential:
    """Mint a coturn REST-API (time-limited) credential scoped to one session.

    coturn (--use-auth-secret) accepts any username of the form
    "<unix-expiry>:<label>" whose password is base64(HMAC-SHA1(secret, username)).
    It rejects the request once the expiry timestamp has passed, so the secret
    itself never has to leave this process.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    username = f"{int(expires_at.timestamp())}:{session_id}"
    digest = hmac.new(shared_secret.encode(), username.encode(), hashlib.sha1).digest()
    password = base64.b64encode(digest).decode()
    return TurnCredential(username=username, password=password, expires_at=expires_at)
