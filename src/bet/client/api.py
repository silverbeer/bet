"""Talking to the BET API: requests, tokens, refresh, and errors as BET errors.

Every failure surfaces as the ``BetError`` subclass the server raised -- its
body names it (``{"error": {"type", "message", "remediation"}}``, see
``bet.api.errors``) -- so a remote command exits with the same code and prints
the same text as the local one. A server that cannot be reached at all is an
``ApiUnreachableError`` naming the URL, never a traceback.

Authentication (ADR 0002 section 4): each call carries the stored access
token. A 401 is answered by one refresh -- which rotates the refresh token, so
the new one is stored at once -- and one retry. If the refresh is refused the
stored tokens are deleted and the user is told to ``bet login``.

Neither a password nor a token is ever put in an exception, a log line or a
``repr``.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any

import httpx2 as httpx
from pydantic import ValidationError

from bet.client.tokens import Session, TokenStore
from bet.errors import (
    ApiUnreachableError,
    AuthenticationError,
    BetError,
    ConfigError,
    DatabaseError,
    DataLocationError,
    NotFoundError,
    UsageError,
)
from bet.services.auth import TokenPair

# Connecting is the step that hangs when Tailscale is down; fail it fast. The
# rest is generous enough for a 20 MiB screenshot over a slow link.
TIMEOUT = httpx.Timeout(30.0, connect=5.0)

LOGIN_REMEDIATION = "Run `bet login` to sign in to {url}."


class RemoteUnknownLabelError(UsageError):
    """The server's ``UnknownLabelError``: its text carries a user-entered label.

    Kept distinct so a front end that escapes labels locally (``bet balance
    add``, ``bet deposit``) can escape them here too, and print the same text.
    """


# The server's error ``type`` -> the class raised here. Its own subclasses of
# UsageError (413, 415) and FastAPI's refusals are the caller's input too.
ERROR_TYPES: dict[str, type[BetError]] = {
    "UsageError": UsageError,
    "PayloadTooLargeError": UsageError,
    "UnsupportedMediaTypeError": UsageError,
    "ValidationError": UsageError,
    "MethodNotAllowedError": UsageError,
    "UnknownLabelError": RemoteUnknownLabelError,
    "NotFoundError": NotFoundError,
    "AuthenticationError": AuthenticationError,
    "DatabaseError": DatabaseError,
    "ConfigError": ConfigError,
    "DataLocationError": DataLocationError,
}

# For a body that names no type this client knows.
ERROR_BY_STATUS: dict[int, type[BetError]] = {
    400: UsageError,
    401: AuthenticationError,
    404: NotFoundError,
    413: UsageError,
    415: UsageError,
    422: UsageError,
    503: DatabaseError,
}


def http_client(api_url: str) -> AbstractContextManager[httpx.Client]:
    """The HTTP client for ``api_url``. Tests replace this with an in-process app."""
    return httpx.Client(base_url=api_url, timeout=TIMEOUT)


def unreachable(api_url: str, exc: Exception) -> ApiUnreachableError:
    reason = str(exc) or type(exc).__name__
    return ApiUnreachableError(
        f"cannot reach the BET API at {api_url}: {reason}",
        remediation=(
            "Check that Tailscale is connected and the server is up (`bet doctor` reports\n"
            "its health), and that api_url is right (`bet config show`). Unset api_url to\n"
            "use a warehouse on this machine instead."
        ),
    )


def error_from(response: httpx.Response, api_url: str) -> BetError:
    """The ``BetError`` an error response describes."""
    try:
        body = response.json()
        error = body["error"]
        name, message = str(error["type"]), str(error["message"])
        remediation = error.get("remediation")
        details = error.get("details")
    except ValueError, KeyError, TypeError:
        return BetError(
            f"the BET API at {api_url} answered HTTP {response.status_code}.",
            remediation="Check the server's logs; api_url may name something else.",
        )
    if isinstance(details, list) and details:
        # Messages only: the server never echoes the rejected input, and nor
        # does this.
        listed = "; ".join(str(d.get("msg", "")) for d in details if isinstance(d, dict))
        remediation = f"{remediation} {listed}".strip() if remediation else listed
    error_type = ERROR_TYPES.get(name) or ERROR_BY_STATUS.get(response.status_code, BetError)
    if error_type is AuthenticationError:
        remediation = LOGIN_REMEDIATION.format(url=api_url)
    return error_type(message, remediation=str(remediation) if remediation else None)


def _params(params: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if params is None:
        return None
    return {key: value for key, value in params.items() if value is not None}


class ApiClient:
    """Calls to one BET API as the user whose tokens ``tokens`` holds."""

    def __init__(self, api_url: str, http: httpx.Client, tokens: TokenStore) -> None:
        self.api_url = api_url
        self.http = http
        self.tokens = tokens

    def __repr__(self) -> str:
        return f"ApiClient({self.api_url!r})"

    # ------------------------------------------------------------ transport

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        try:
            return self.http.request(
                method,
                path,
                params=_params(params),
                json=json,
                content=content,
                headers=dict(headers or {}),
            )
        except httpx.TransportError as exc:
            raise unreachable(self.api_url, exc) from None

    def _result(self, response: httpx.Response) -> Any:
        if response.is_success:
            return None if response.status_code == 204 or not response.content else response.json()
        raise error_from(response, self.api_url)

    def call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        auth: bool = True,
    ) -> Any:
        """Send one request and return its JSON, or raise the error it carried."""
        if not auth:
            return self._result(
                self._send(method, path, params=params, json=json, content=content, headers=headers)
            )

        session = self.require_session()
        for attempt in range(2):
            response = self._send(
                method,
                path,
                params=params,
                json=json,
                content=content,
                headers={**(headers or {}), "Authorization": f"Bearer {session.access_token}"},
            )
            if response.status_code != 401 or attempt:
                break
            session = self._refresh(session)
        return self._result(response)

    def get(self, path: str, **params: Any) -> Any:
        return self.call("GET", path, params=params)

    def post(self, path: str, body: Any = None, **params: Any) -> Any:
        return self.call("POST", path, params=params, json=body)

    # --------------------------------------------------------------- tokens

    def require_session(self) -> Session:
        session = self.tokens.load()
        if session is None:
            raise AuthenticationError(
                f"not logged in to {self.api_url}.",
                remediation=LOGIN_REMEDIATION.format(url=self.api_url),
            )
        return session

    def _refresh(self, session: Session) -> Session:
        """Exchange the refresh token for a new pair, and store the pair at once.

        Refresh tokens rotate: the presented one stops working, so the new one
        must be saved before anything else can fail.
        """
        response = self._send(
            "POST", "/auth/refresh", json={"refresh_token": session.refresh_token}
        )
        if response.status_code == 401:
            self.tokens.clear()
            raise AuthenticationError(
                f"your session with {self.api_url} has expired or was revoked.",
                remediation=LOGIN_REMEDIATION.format(url=self.api_url),
            )
        pair = self._pair(response)
        renewed = Session(session.username, pair.access_token, pair.refresh_token)
        self.tokens.save(renewed)
        return renewed

    def _pair(self, response: httpx.Response) -> TokenPair:
        body = self._result(response)
        try:
            return TokenPair.model_validate(body)
        except ValidationError:
            # Never chained or shown: a validation error quotes its input,
            # and this input is tokens.
            raise BetError(
                f"the BET API at {self.api_url} answered a login without a usable token pair.",
                remediation="Check that api_url names a BET API, and the server's version.",
            ) from None

    def login(self, username: str, password: str) -> Session:
        """Exchange a user name and password for tokens, and store them."""
        response = self._send(
            "POST", "/auth/token", json={"username": username, "password": password}
        )
        if response.status_code == 401:
            raise AuthenticationError(
                error_from(response, self.api_url).message,
                remediation=(
                    "Check both and retry. The operator sets passwords with\n"
                    "`bet user set-password` where the warehouse lives."
                ),
            )
        pair = self._pair(response)
        session = Session(username, pair.access_token, pair.refresh_token)
        self.tokens.save(session)
        return session

    def revoke(self, session: Session) -> None:
        """Revoke ``session``'s refresh token on the server.

        An already-expired or revoked token is the outcome asked for, not an
        error.
        """
        response = self._send("POST", "/auth/logout", json={"refresh_token": session.refresh_token})
        if response.status_code != 401:
            self._result(response)


__all__ = [
    "ERROR_TYPES",
    "TIMEOUT",
    "ApiClient",
    "RemoteUnknownLabelError",
    "error_from",
    "http_client",
    "unreachable",
]
