"""Envelope encryption for persisted YouTube OAuth credentials.

A refresh token is a long-lived bearer credential for someone's YouTube
channel. It is never written to a normal database column, a log record, a
contract, an API response, Temporal history or an asset. Instead it is sealed
here with AES-256-GCM and stored as ciphertext plus its metadata, in a table
separate from the canonical connection row.

Three properties are load bearing:

* **Authenticated encryption with associated data.** The connection's UUID and
  the credential's purpose are bound in as AAD, so a ciphertext lifted from one
  connection cannot be replayed into another - the tag check fails.
* **A random 96-bit nonce per seal.** Never derived, never reused, stored
  beside the ciphertext.
* **A versioned key.** Every ciphertext records the key version that sealed it,
  and a :class:`Keyring` decrypts with any retired key while sealing only with
  the active one. That is what makes rotation a configuration change rather
  than a migration.

Every failure raises :class:`CredentialCipherError`, whose message names the key
version and nothing else. A cryptography exception carrying key or ciphertext
material never propagates.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-256. Anything shorter is refused at construction rather than at first use.
KEY_BYTES: Final = 32
#: 96 bits: the nonce size AES-GCM is specified for.
NONCE_BYTES: Final = 12

#: Associated-data purposes. A refresh token and a resumable session URI are
#: sealed under different purposes, so neither can be substituted for the other.
PURPOSE_REFRESH_TOKEN: Final = "youtube.refresh_token"
PURPOSE_ACCESS_TOKEN: Final = "youtube.access_token"
PURPOSE_PKCE_VERIFIER: Final = "youtube.pkce_verifier"
PURPOSE_SESSION_URI: Final = "youtube.resumable_session_uri"

#: The development-only key an unconfigured local environment falls back to.
#: Deliberately obvious. :func:`keyring_from_environment` refuses to use it
#: unless development mode is explicitly requested.
DEVELOPMENT_KEY_VERSION: Final = "dev-insecure-1"
_DEVELOPMENT_KEY: Final = b"vidgen-local-development-only!!!\x00"[:KEY_BYTES]


class CredentialCipherError(RuntimeError):
    """A sealing or opening failure, redacted by construction.

    The message names the key version and the purpose. It never contains key
    material, ciphertext, plaintext or a provider payload.
    """


class SecretValue:
    """A string that does not leak through ``repr``, ``str`` or a traceback.

    Used for every decrypted token between the cipher and the HTTP client, so
    an unexpected exception, a logging call or a Pydantic model dump cannot
    print it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the underlying value. The only way to read it."""
        return self._value

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "SecretValue(***)"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return "***"

    def __bool__(self) -> bool:
        return bool(self._value)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SecretValue) and self._value == other._value

    def __hash__(self) -> int:
        return hash(("SecretValue", self._value))


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """One AES-GCM ciphertext with everything needed to open it but the key."""

    ciphertext: bytes
    nonce: bytes
    key_version: str
    purpose: str

    def __post_init__(self) -> None:
        if len(self.nonce) != NONCE_BYTES:
            raise CredentialCipherError(
                f"a sealed secret for {self.purpose!r} must carry a {NONCE_BYTES}-byte nonce"
            )
        if not self.ciphertext:
            raise CredentialCipherError(f"a sealed secret for {self.purpose!r} may not be empty")

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"SealedSecret(purpose={self.purpose!r}, key_version={self.key_version!r}, "
            f"bytes={len(self.ciphertext)})"
        )


def _decode_key(version: str, encoded: str) -> bytes:
    """Decode one configured key, accepting base64 with or without padding."""
    raw = encoded.strip()
    if not raw:
        raise CredentialCipherError(f"encryption key {version!r} is empty")
    padding = "=" * (-len(raw) % 4)
    try:
        key = base64.urlsafe_b64decode(raw + padding)
    except (ValueError, TypeError):
        try:
            key = base64.b64decode(raw + padding)
        except (ValueError, TypeError) as error:  # pragma: no cover - defensive
            raise CredentialCipherError(
                f"encryption key {version!r} is not valid base64"
            ) from error
    if len(key) != KEY_BYTES:
        raise CredentialCipherError(
            f"encryption key {version!r} must decode to exactly {KEY_BYTES} bytes"
        )
    return key


class Keyring:
    """The active sealing key plus every retired key still needed to decrypt."""

    def __init__(self, keys: dict[str, bytes], active_version: str) -> None:
        if not keys:
            raise CredentialCipherError("a keyring requires at least one key")
        if active_version not in keys:
            raise CredentialCipherError(
                f"the active key version {active_version!r} is not configured"
            )
        for version, key in keys.items():
            if len(key) != KEY_BYTES:
                raise CredentialCipherError(
                    f"encryption key {version!r} must be exactly {KEY_BYTES} bytes"
                )
        self._keys = dict(keys)
        self._active = active_version

    @property
    def active_version(self) -> str:
        return self._active

    @property
    def versions(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    @property
    def is_development_only(self) -> bool:
        """Whether any configured key is the shared development key."""
        return DEVELOPMENT_KEY_VERSION in self._keys

    def _key(self, version: str) -> bytes:
        try:
            return self._keys[version]
        except KeyError as error:
            raise CredentialCipherError(
                f"no encryption key configured for version {version!r}; "
                "a rotated-out key must stay configured until every ciphertext is re-sealed"
            ) from error

    def with_key(self, version: str, key: bytes, *, activate: bool = True) -> Keyring:
        """Return a keyring that adds ``version``, optionally as the new active key."""
        keys = dict(self._keys)
        keys[version] = key
        return Keyring(keys, version if activate else self._active)

    @staticmethod
    def _aad(purpose: str, context: str) -> bytes:
        return f"{purpose}|{context}".encode()

    def seal(self, plaintext: str, *, purpose: str, context: str) -> SealedSecret:
        """Seal ``plaintext`` under the active key, binding ``purpose`` and ``context``."""
        nonce = os.urandom(NONCE_BYTES)
        try:
            ciphertext = AESGCM(self._key(self._active)).encrypt(
                nonce, plaintext.encode(), self._aad(purpose, context)
            )
        except Exception as error:  # pragma: no cover - defensive
            raise CredentialCipherError(
                f"failed to seal {purpose!r} with key version {self._active!r}"
            ) from error
        return SealedSecret(
            ciphertext=ciphertext, nonce=nonce, key_version=self._active, purpose=purpose
        )

    def open(self, sealed: SealedSecret, *, context: str) -> SecretValue:
        """Open ``sealed``, verifying the tag over ``purpose`` and ``context``."""
        key = self._key(sealed.key_version)
        try:
            plaintext = AESGCM(key).decrypt(
                sealed.nonce, sealed.ciphertext, self._aad(sealed.purpose, context)
            )
        except InvalidTag as error:
            raise CredentialCipherError(
                f"the sealed {sealed.purpose!r} value failed authentication under key version "
                f"{sealed.key_version!r}; it was sealed for a different connection, purpose or key"
            ) from error
        except Exception as error:
            raise CredentialCipherError(
                f"failed to open {sealed.purpose!r} sealed with key version {sealed.key_version!r}"
            ) from error
        return SecretValue(plaintext.decode())

    def reseal(self, sealed: SealedSecret, *, context: str) -> SealedSecret:
        """Re-seal an existing ciphertext under the active key.

        This is the whole of key rotation: open with the version recorded on the
        row, seal with the active version, write both back in one transaction.
        """
        if sealed.key_version == self._active:
            return sealed
        opened = self.open(sealed, context=context)
        return self.seal(opened.reveal(), purpose=sealed.purpose, context=context)


def development_keyring() -> Keyring:
    """The shared, deliberately insecure local development keyring.

    Never suitable for a shared or deployed environment: the key is in this
    source file, so anyone with the repository can decrypt anything sealed with
    it. A deployment resolves a real key from Key Vault instead.
    """
    return Keyring({DEVELOPMENT_KEY_VERSION: _DEVELOPMENT_KEY}, DEVELOPMENT_KEY_VERSION)


def keyring_from_environment(
    *,
    key: str | None = None,
    key_version: str | None = None,
    retired_keys: str | None = None,
    allow_development_key: bool = False,
) -> Keyring:
    """Build a keyring from configuration.

    ``key`` is the base64 active key and ``key_version`` names it. ``retired_keys``
    is an optional ``version:base64,version:base64`` list of keys that must stay
    decryptable until every ciphertext has been re-sealed.

    With no key configured this raises, unless ``allow_development_key`` is set,
    which is what local development and the test suites opt into explicitly.
    """
    keys: dict[str, bytes] = {}
    for entry in (retired_keys or "").split(","):
        item = entry.strip()
        if not item:
            continue
        version, _, encoded = item.partition(":")
        if not version or not encoded:
            raise CredentialCipherError(
                "retired encryption keys must be given as 'version:base64,version:base64'"
            )
        keys[version.strip()] = _decode_key(version.strip(), encoded)

    if key and key.strip():
        version = (key_version or "").strip()
        if not version:
            raise CredentialCipherError(
                "VIDGEN_YOUTUBE_TOKEN_ENCRYPTION_KEY_VERSION must name the configured key"
            )
        keys[version] = _decode_key(version, key)
        return Keyring(keys, version)

    if not allow_development_key:
        raise CredentialCipherError(
            "VIDGEN_YOUTUBE_TOKEN_ENCRYPTION_KEY is required to store YouTube credentials; "
            "in a deployment it is resolved from Key Vault by the workload managed identity"
        )
    keys[DEVELOPMENT_KEY_VERSION] = _DEVELOPMENT_KEY
    return Keyring(keys, DEVELOPMENT_KEY_VERSION)


def generate_key() -> str:
    """A fresh base64 AES-256 key, for the bootstrap documentation and tests."""
    return base64.urlsafe_b64encode(os.urandom(KEY_BYTES)).decode().rstrip("=")


def connection_context(connection_id: object) -> str:
    """The AAD context binding a ciphertext to one connection row."""
    return f"connection:{connection_id}"


def session_context(publication_run_id: object) -> str:
    """The AAD context binding a sealed session URI to one publication run."""
    return f"publication:{publication_run_id}"


def state_context(state_id: object) -> str:
    """The AAD context binding a sealed PKCE verifier to one OAuth state row."""
    return f"oauth-state:{state_id}"
