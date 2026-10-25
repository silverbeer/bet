"""Suite-wide fixtures.

Every test runs against an in-memory keyring, so nothing here can read or
write the real macOS Keychain -- ``bet login`` in a test stores its tokens in a
dict that is thrown away with the test.
"""

from __future__ import annotations

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class MemoryKeyring(KeyringBackend):
    """A keyring held in a dict."""

    priority = 1

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.items[(service, username)]
        except KeyError:
            raise PasswordDeleteError(username) from None


@pytest.fixture(autouse=True)
def memory_keyring() -> MemoryKeyring:
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    return backend
