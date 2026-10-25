"""Where a login's tokens are kept: the OS keychain, through ``keyring``.

On macOS that is the login Keychain. Tokens never go in the config file, an
environment variable or a command line (ADR 0002 section 3); ``security add-
generic-password -w <token>`` is avoided for the same reason -- it puts the
secret in ``ps``.

One Keychain item per API: service ``bet-api``, account the ``api_url``, and
the item's secret a small JSON document holding the user name and both
tokens. Keyed by URL so a laptop can be logged in to a dev and a prod server
at once; one user per server at a time, which is all ``bet login`` offers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

from bet.errors import ConfigError

SERVICE = "bet-api"

KEYCHAIN_REMEDIATION = (
    "Unlock the login Keychain (Keychain Access) and retry. Off macOS, install a "
    "keyring backend, or set PYTHON_KEYRING_BACKEND."
)


@dataclass(frozen=True, slots=True)
class Session:
    """A logged-in user's tokens. Neither token appears in ``repr``."""

    username: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)


def _keychain_error(action: str) -> ConfigError:
    # The keyring exception is deliberately not chained into the message: its
    # text is the backend's, and nothing guarantees it leaves the secret out.
    return ConfigError(f"cannot {action} the keychain.", remediation=KEYCHAIN_REMEDIATION)


class TokenStore:
    """The tokens for one ``api_url``."""

    def __init__(self, api_url: str) -> None:
        self.api_url = api_url

    def __repr__(self) -> str:
        return f"TokenStore({self.api_url!r})"

    def load(self) -> Session | None:
        """The stored session, or ``None`` when there is none (or it is unreadable)."""
        try:
            raw = keyring.get_password(SERVICE, self.api_url)
        except KeyringError:
            raise _keychain_error("read") from None
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return Session(
                username=str(data["username"]),
                access_token=str(data["access_token"]),
                refresh_token=str(data["refresh_token"]),
            )
        except ValueError, KeyError, TypeError:
            # Written by something else, or a format this version does not know:
            # treat it as logged out rather than fail every command.
            return None

    def save(self, session: Session) -> None:
        document = json.dumps(
            {
                "username": session.username,
                "access_token": session.access_token,
                "refresh_token": session.refresh_token,
            }
        )
        try:
            keyring.set_password(SERVICE, self.api_url, document)
        except KeyringError:
            raise _keychain_error("write to") from None

    def clear(self) -> bool:
        """Delete the stored session. ``True`` if there was one."""
        try:
            keyring.delete_password(SERVICE, self.api_url)
        except PasswordDeleteError:
            return False
        except KeyringError:
            raise _keychain_error("delete from") from None
        return True


__all__ = ["SERVICE", "Session", "TokenStore"]
