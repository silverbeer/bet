"""Passwords and BET-issued tokens (ADR 0002 section 4).

Passwords are hashed with argon2id (argon2-cffi's defaults, the RFC 9106
low-memory profile). A login presents a display name and a password and
receives two JWTs:

* an **access** token, short-lived (15 minutes by default), sent as
  ``Authorization: Bearer`` on every request. It is not stored: it is checked
  by signature, expiry and ``typ``, and the user it names must still exist and
  be active.
* a **refresh** token, long-lived (30 days by default), exchanged for a new
  pair. Each one is recorded -- by ``jti`` and a SHA-256 digest, never the
  token itself -- so it can be rotated and revoked. A refresh revokes the
  presented token; presenting a revoked one again means it was copied, and
  everything issued from it is revoked too.

Claims are ``sub`` (user id), ``tid`` (tenant id), ``iat``, ``exp``, ``jti`` and
``typ``. The request's ``OwnerScope`` is built from ``tid`` and ``sub`` alone.

HS256 because the issuer and the only verifier are the same process: there is
no third party to hand a public key to, so an asymmetric algorithm would add a
key pair to manage and buy nothing. Decoding accepts HS256 only, which rules
out ``alg: none`` and algorithm-confusion tokens.

Failures raise ``AuthenticationError`` with deliberately generic messages: a
login never says whether the user exists, and an unknown user costs the same
argon2 verification as a wrong password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from bet.database import identity
from bet.database.repository import Warehouse
from bet.errors import AuthenticationError, UsageError
from bet.models.credential import RefreshToken, UserCredential
from bet.models.ownership import OwnerScope

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

ALGORITHM = "HS256"
# RFC 7518 section 3.2: an HMAC key at least as long as the hash output.
MIN_SIGNING_KEY_BYTES = 32
DEFAULT_ACCESS_TTL = timedelta(minutes=15)
DEFAULT_REFRESH_TTL = timedelta(days=30)
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024

TokenType = Literal["access", "refresh"]
CLAIMS = ("sub", "tid", "iat", "exp", "jti", "typ")

INVALID_LOGIN = "invalid username or password."
INVALID_TOKEN = "the token is invalid."
EXPIRED_TOKEN = "the token has expired."

_hasher = PasswordHasher()


# ---------------------------------------------------------------- settings


class TokenSettings(BaseModel):
    """The signing key and token lifetimes. The key never reaches a repr or a log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signing_key: SecretStr
    access_ttl: timedelta = DEFAULT_ACCESS_TTL
    refresh_ttl: timedelta = DEFAULT_REFRESH_TTL

    @field_validator("signing_key", mode="after")
    @classmethod
    def _long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value().encode()) < MIN_SIGNING_KEY_BYTES:
            raise ValueError(f"the signing key must be at least {MIN_SIGNING_KEY_BYTES} bytes")
        return value

    @field_validator("access_ttl", "refresh_ttl", mode="after")
    @classmethod
    def _positive(cls, value: timedelta) -> timedelta:
        if value <= timedelta(0):
            raise ValueError("a token lifetime must be positive")
        return value


class TokenPair(BaseModel):
    """What a login or a refresh returns. Lifetimes are in seconds."""

    model_config = ConfigDict(frozen=True)

    access_token: Annotated[str, Field(repr=False)]
    refresh_token: Annotated[str, Field(repr=False)]
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    refresh_expires_in: int


class Claims(BaseModel):
    """A verified token's claims."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sub: UUID
    tid: UUID
    iat: int
    exp: int
    jti: UUID
    typ: TokenType

    @property
    def scope(self) -> OwnerScope:
        return OwnerScope(tenant_id=self.tid, user_id=self.sub)


# --------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    """An argon2id PHC string. Refuses a password outside the length bounds."""
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise UsageError(
            f"a password must be {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.",
            remediation="Choose a longer password.",
        )
    return _hasher.hash(password)


def password_matches(password_hash: str, password: str) -> bool:
    """argon2's own verification, which compares in constant time."""
    try:
        return _hasher.verify(password_hash, password)
    except VerificationError, InvalidHashError:
        return False


@cache
def _dummy_hash() -> str:
    """A hash of nothing anyone knows, verified against for an unknown user."""
    return _hasher.hash(secrets.token_urlsafe(32))


def warm_up() -> None:
    """Compute the dummy hash now, so the first unknown-user login is not faster."""
    _dummy_hash()


def set_password(w: Warehouse, password: str, *, now: datetime | None = None) -> UserCredential:
    """Set or rotate the scoped user's password."""
    password_hash = hash_password(password)
    with w.transaction():
        return w.credentials.set_hash(password_hash, now or datetime.now(UTC))


def verify_password(w: Warehouse, password: str) -> bool:
    """Whether ``password`` is the scoped user's. False, after equal work, with no credential."""
    credential = w.credentials.current()
    if credential is None:
        password_matches(_dummy_hash(), password)
        return False
    return password_matches(credential.password_hash, password)


# ------------------------------------------------------------------ tokens


def _encode(
    settings: TokenSettings, scope: OwnerScope, typ: TokenType, jti: UUID, now: datetime
) -> str:
    ttl = settings.access_ttl if typ == "access" else settings.refresh_ttl
    claims = {
        "sub": str(scope.user_id),
        "tid": str(scope.tenant_id),
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
        "jti": str(jti),
        "typ": typ,
    }
    return jwt.encode(claims, settings.signing_key.get_secret_value(), algorithm=ALGORITHM)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_pair(
    w: Warehouse, settings: TokenSettings, now: datetime
) -> tuple[TokenPair, RefreshToken]:
    """Mint a pair, and the refresh-token row the caller must store."""
    refresh_id = uuid.uuid7()
    access = _encode(settings, w.scope, "access", uuid.uuid7(), now)
    refresh = _encode(settings, w.scope, "refresh", refresh_id, now)
    row = RefreshToken(
        tenant_id=w.scope.tenant_id,
        user_id=w.scope.user_id,
        id=refresh_id,
        token_hash=_digest(refresh),
        issued_at=now,
        expires_at=now + settings.refresh_ttl,
    )
    pair = TokenPair(
        access_token=access,
        refresh_token=refresh,
        expires_in=int(settings.access_ttl.total_seconds()),
        refresh_expires_in=int(settings.refresh_ttl.total_seconds()),
    )
    return pair, row


def issue_tokens(
    w: Warehouse, settings: TokenSettings, *, now: datetime | None = None
) -> TokenPair:
    """A fresh pair for the scoped user, starting a new refresh chain."""
    pair, row = _new_pair(w, settings, now or datetime.now(UTC))
    with w.transaction():
        w.refresh_tokens.add(row)
    return pair


def decode(token: str, settings: TokenSettings, typ: TokenType) -> Claims:
    """Verify signature, expiry, every required claim and the token type."""
    try:
        payload = jwt.decode(
            token,
            settings.signing_key.get_secret_value(),
            algorithms=[ALGORITHM],
            options={"require": list(CLAIMS)},
        )
        claims = Claims.model_validate(payload)
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError(EXPIRED_TOKEN, remediation=_remediation(typ)) from exc
    except (jwt.InvalidTokenError, ValidationError) as exc:
        raise AuthenticationError(INVALID_TOKEN, remediation=_remediation(typ)) from exc
    if claims.typ != typ:
        raise AuthenticationError(INVALID_TOKEN, remediation=_remediation(typ))
    return claims


def _remediation(typ: TokenType) -> str:
    if typ == "access":
        return "Send a current access token, or refresh it at POST /auth/refresh."
    return "Log in again at POST /auth/token."


def _require_active(conn: DuckDBPyConnection, scope: OwnerScope, typ: TokenType) -> None:
    user = identity.find_user(conn, scope)
    if user is None or user.status != "active":
        raise AuthenticationError(INVALID_TOKEN, remediation=_remediation(typ))


# ------------------------------------------------------------------ flows


def login(
    conn: DuckDBPyConnection,
    username: str,
    password: str,
    settings: TokenSettings,
    *,
    now: datetime | None = None,
) -> TokenPair:
    """Exchange a display name and password for a token pair.

    Every failure -- no such user, an ambiguous name, a disabled user, no
    password set, the wrong password -- is the same error after the same one
    argon2 verification.
    """
    matches = identity.users_named(conn, username.strip())
    user = matches[0] if len(matches) == 1 else None
    w = Warehouse(conn, OwnerScope(tenant_id=user.tenant_id, user_id=user.id)) if user else None
    credential = w.credentials.current() if w is not None else None

    if credential is None:
        password_matches(_dummy_hash(), password)
        raise AuthenticationError(INVALID_LOGIN)
    if not password_matches(credential.password_hash, password):
        raise AuthenticationError(INVALID_LOGIN)
    assert user is not None and w is not None
    if user.status != "active":
        raise AuthenticationError(INVALID_LOGIN)
    return issue_tokens(w, settings, now=now)


def authenticate(
    conn: DuckDBPyConnection, access_token: str, settings: TokenSettings
) -> OwnerScope:
    """The scope an access token names, provided its user still exists and is active."""
    scope = decode(access_token, settings, "access").scope
    _require_active(conn, scope, "access")
    return scope


def _stored(w: Warehouse, claims: Claims, token: str) -> RefreshToken:
    row = w.refresh_tokens.find(claims.jti)
    if row is None or not hmac.compare_digest(row.token_hash, _digest(token)):
        raise AuthenticationError(INVALID_TOKEN, remediation=_remediation("refresh"))
    return row


def refresh(
    conn: DuckDBPyConnection,
    refresh_token: str,
    settings: TokenSettings,
    *,
    now: datetime | None = None,
) -> TokenPair:
    """Rotate: revoke the presented refresh token and issue a new pair in its chain.

    A token that was already revoked is a replay. Whatever was issued from it
    is revoked as well, so the legitimate holder and the copy are both logged
    out, and the next login starts clean.
    """
    at = now or datetime.now(UTC)
    claims = decode(refresh_token, settings, "refresh")
    _require_active(conn, claims.scope, "refresh")
    w = Warehouse(conn, claims.scope)
    old = _stored(w, claims, refresh_token)

    pair, row = _new_pair(w, settings, at)
    with w.transaction():
        rotated = w.refresh_tokens.revoke(old.id, at, replaced_by=row.id)
        if rotated:
            w.refresh_tokens.add(row)
    if not rotated:
        with w.transaction():
            w.refresh_tokens.revoke_chain(old.id, at)
        raise AuthenticationError(
            "the refresh token was already used; this session has been revoked.",
            remediation="Log in again at POST /auth/token.",
        )
    return pair


def logout(
    conn: DuckDBPyConnection,
    refresh_token: str,
    settings: TokenSettings,
    *,
    now: datetime | None = None,
) -> None:
    """Revoke a refresh token. Already revoked is not an error.

    Access tokens are not stored, so one already issued stays valid until it
    expires -- at most the access lifetime.
    """
    claims = decode(refresh_token, settings, "refresh")
    w = Warehouse(conn, claims.scope)
    old = _stored(w, claims, refresh_token)
    with w.transaction():
        w.refresh_tokens.revoke(old.id, now or datetime.now(UTC))


__all__ = [
    "ALGORITHM",
    "DEFAULT_ACCESS_TTL",
    "DEFAULT_REFRESH_TTL",
    "MIN_PASSWORD_LENGTH",
    "MIN_SIGNING_KEY_BYTES",
    "Claims",
    "TokenPair",
    "TokenSettings",
    "authenticate",
    "decode",
    "hash_password",
    "issue_tokens",
    "login",
    "logout",
    "password_matches",
    "refresh",
    "set_password",
    "verify_password",
    "warm_up",
]
