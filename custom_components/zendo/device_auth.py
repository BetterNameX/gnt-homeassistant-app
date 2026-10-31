"""Device control backend tokens, device registrations and command envelopes.

Plain logic with no Home Assistant import, so the tests can load this module on
its own. ``device_api.py`` wires it into Home Assistant. See
docs/managed-devices.md, "Device Registration (v2)".

- **Signing keys** come from one DNS TXT record, ``{"keys": [...], "ttl": ...}``.
  Each key is a P-256 public key, base64 of its DER SPKI, with a ``kid`` (the
  first 8 bytes of SHA-256 over that DER, as hex) and a window, ``nbf`` to
  ``exp``, in unix seconds.
- **A device control backend token** is an ES256 JWT for this site, naming a
  profile and a device, and with the admin scope for an admin's device. It
  also carries the device's encryption public key, a P-256 key.
- **A registration** ties one device to an API key, kept only as its SHA-256
  hash, and to the encryption public key from its token. A revoked device keeps
  a tombstone, and can't register again.
- **ecies-p256** encrypts to a device's public key: a one-off P-256 key, ECDH,
  HKDF-SHA256 and AES-256-GCM, with additional data the receiver checks.
- **A command envelope** carries a JSON-RPC request to one device: in clear, or
  encrypted to the device's public key, with the envelope's clear fields as
  additional data.
- **A sealed object** (``{"scheme": "ecies-p256", "data": ...}``) carries
  some of a message's fields encrypted to the device's public key, the rest of
  the message staying in clear: a registered device's key, or the key in the
  token of an admin's call.
- **The site config encryption key** held is the stored one, otherwise the one
  in the ``input_text`` helper's state, otherwise none.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import jwt
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

TOKEN_ISSUER = "https://device-control-backend.production.gnt.betterna.me"
TOKEN_AUDIENCE = "api:device-control-backend"
ALG_ES256 = "ES256"
ADMIN_SCOPE = "device-control-backend:admin"

# How far a hub's clock may be off from the cloud's when checking a token's
# expiry.
CLOCK_LEEWAY = 60

SCHEME_NONE = "none"
SCHEME_ECIES_P256 = "ecies-p256"
# The encryption schemes this integration can use, in its order of preference.
SUPPORTED_SCHEMES = (SCHEME_ECIES_P256,)

# Methods whose request is always encrypted for a registered device, and never
# sent to a device that hasn't registered (``may_send_command``). The Zendo app
# refuses them in clear, so keep this in sync with the app and the Homey app.
MUST_ENCRYPT_METHODS = frozenset({"adminCode.set", "app.lock"})

# The methods named in a sealed answer's additional data: the settings restore,
# an admin's lookup of a managed device's admin code, and reading or claiming
# the site config encryption key. The Zendo app and the Homey app use the same
# strings.
SEALED_METHOD_SETTINGS_RESTORE = "settingsRestore.get"
SEALED_METHOD_ADMIN_CODE_GET = "adminCode.get"
SEALED_METHOD_ENCRYPTION_KEY_GET = "encryptionKey.get"

# An admin's lookup of an admin code when there's none to hand out, before it's
# sealed.
UNKNOWN_ADMIN_CODE_LOOKUP: dict[str, Any] = {
    "status": "unknown",
    "admin_code": None,
    "reported_at": None,
}

# Expected failures, answered as ``{"success": false, "error": <code>}``. The
# Zendo app and the Homey app use the same strings, so keep all three in sync.
ERROR_NOT_LINKED = "not_linked"
ERROR_INVALID_TOKEN = "invalid_token"
ERROR_WRONG_SITE = "wrong_site"
ERROR_NOT_ADMIN = "not_admin"
ERROR_UNKNOWN_API_KEY = "unknown_api_key"
ERROR_DEVICE_REVOKED = "device_revoked"
ERROR_UNSUPPORTED_SCHEME = "unsupported_scheme"
# Registering, or asking for a sealed answer, with a token that carries no
# encryption public key, or one that isn't a P-256 key.
ERROR_PUBLIC_KEY_REQUIRED = "public_key_required"
# zendo/v2/set_up: the cloud didn't confirm the push token's site, or couldn't be
# reached.
ERROR_INVALID_PUSH_TOKEN = "invalid_push_token"
# A v1 call the v1 rules refuse (v1_rules.py).
ERROR_V2_REQUIRED = "v2_required"

# The ``link`` of zendo/v2/setup_status and zendo/v2/set_up. The app and the
# Homey app use the same strings, so keep all three in sync.
LINK_THIS_PROPERTY = "this_property"
LINK_ANOTHER_PROPERTY = "another_property"
LINK_NOT_LINKED = "not_linked"

# The label the Zendo app puts at the end of each long-lived token's client
# name: ``bngnt1:`` and a JSON object with the site (``s``), profile (``p``) and
# device (``d``), or invitation (``i``), it was made for.
TOKEN_LABEL_PREFIX = "bngnt1:"

_NONCE_BYTES = 12
_KEY_BYTES = 32
_POINT_BYTES = 65
_TAG_BYTES = 16
_HKDF_INFO_PREFIX = b"zendo ecies-p256 v1"
_API_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# DNS TXT records
# ---------------------------------------------------------------------------

def parse_txt_data(data: str) -> str:
    """Return a TXT record's text, its character-strings joined.

    A DNS-over-HTTPS JSON answer gives a record either as its presentation form,
    one or more quoted strings separated by spaces with ``\\"``, ``\\\\`` and
    ``\\DDD`` escapes, or already joined. A record longer than 255 bytes always
    comes as several strings.

    Raises ``ValueError`` when the quoting is broken.
    """
    text = data.strip()
    if not text.startswith('"'):
        return text

    parts: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        if text[index].isspace():
            index += 1
            continue
        if text[index] != '"':
            raise ValueError("Text outside a quoted string")
        index += 1
        chars: list[str] = []
        while True:
            if index >= length:
                raise ValueError("Unterminated string")
            char = text[index]
            if char == "\\":
                if index + 1 >= length:
                    raise ValueError("Unterminated escape")
                escaped = text[index + 1 : index + 4]
                if len(escaped) == 3 and escaped.isdigit():
                    chars.append(chr(int(escaped)))
                    index += 4
                else:
                    chars.append(text[index + 1])
                    index += 2
                continue
            if char == '"':
                index += 1
                break
            chars.append(char)
            index += 1
        parts.append("".join(chars))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Signing keys
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SigningKey:
    """A public key the device control backend token may be signed with."""

    kid: str
    # The algorithm the key signs with, one of ``SIGNING_ALGORITHMS``.
    alg: str
    public_key: Any
    nbf: int
    exp: int

    def valid_at(self, now: float) -> bool:
        return self.nbf <= now < self.exp


@dataclass(frozen=True)
class KeysRecord:
    """The signing keys record, as read from DNS."""

    keys: tuple[SigningKey, ...]
    # How long, in seconds, until the record should be read again, or None.
    ttl: int | None
    # How many listed keys were left out because they couldn't be used.
    skipped: int
    # The algorithms of listed keys left out because this version doesn't
    # implement them. Those keys are ignored, not counted in ``skipped``.
    unknown_algorithms: frozenset[str] = frozenset()


def compute_kid(spki_der: bytes) -> str:
    """Return a public key's id: hex of the first 8 bytes of its SHA-256."""
    return hashlib.sha256(spki_der).digest()[:8].hex()


def _es256_public_key(der: bytes) -> Any | None:
    try:
        public_key = serialization.load_der_public_key(der)
    except ValueError:
        return None
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        return None
    return public_key


# The signing algorithms this version implements, each with how to read its
# public key from DER SPKI. A key with any other algorithm is ignored, so the
# record can list a new algorithm's keys before every hub implements it.
_SIGNING_KEY_READERS = {ALG_ES256: _es256_public_key}
SIGNING_ALGORITHMS = frozenset(_SIGNING_KEY_READERS)


def _parse_signing_key(item: dict[str, Any], alg: str) -> SigningKey | None:
    kid = item.get("kid")
    key = item.get("key")
    nbf = item.get("nbf")
    exp = item.get("exp")
    if not (isinstance(kid, str) and isinstance(key, str) and _is_int(nbf) and _is_int(exp)):
        return None

    try:
        der = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError):
        return None
    if compute_kid(der) != kid:
        return None

    public_key = _SIGNING_KEY_READERS[alg](der)
    if public_key is None:
        return None
    return SigningKey(kid=kid, alg=alg, public_key=public_key, nbf=nbf, exp=exp)


def parse_keys_record(content: str) -> KeysRecord:
    """Parse the signing keys record's JSON.

    A key whose algorithm this version doesn't implement is ignored, and its
    algorithm listed in ``unknown_algorithms``. A key that can't be read, or
    whose ``kid`` isn't its own, is left out and counted in ``skipped``.
    Raises ``ValueError`` when the record itself isn't ``{"keys": [...]}``.
    """
    data = json.loads(content)
    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        # ValueError, like a record that isn't JSON: callers handle both alike.
        raise ValueError("Not a signing keys record")  # noqa: TRY004

    keys: list[SigningKey] = []
    skipped = 0
    unknown_algorithms: set[str] = set()
    for item in data["keys"]:
        alg = item.get("alg") if isinstance(item, dict) else None
        if not isinstance(alg, str):
            skipped += 1
            continue
        if alg not in SIGNING_ALGORITHMS:
            unknown_algorithms.add(alg)
            continue
        key = _parse_signing_key(item, alg)
        if key is None:
            skipped += 1
        else:
            keys.append(key)

    ttl = data.get("ttl")
    return KeysRecord(
        keys=tuple(keys),
        ttl=ttl if _is_int(ttl) and ttl > 0 else None,
        skipped=skipped,
        unknown_algorithms=frozenset(unknown_algorithms),
    )


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TokenClaims:
    """What a checked device control backend token says."""

    site_id: str
    profile_id: str
    device_id: str
    admin: bool
    # The device's encryption public key, base64 of its DER SPKI, as the token
    # gives it, or None when it gives none. Read it with ``parse_public_key``.
    encryption_public_key: str | None = None


class TokenError(Exception):
    """A token that can't be used, with the error code to answer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def token_kid(token: Any) -> str | None:
    """Return the ``kid`` a token's header names, without checking anything."""
    if not isinstance(token, str):
        return None
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        return None
    kid = header.get("kid")
    return kid if isinstance(kid, str) else None


def check_token(
    token: Any,
    keys: Iterable[SigningKey],
    now: float,
    site_id: str | None,
) -> TokenClaims:
    """Check a device control backend token, and return its claims.

    The token's header names its key by ``kid``. That key must be valid at
    ``now``, and the token is checked with the key's own algorithm: a header
    ``alg`` that isn't the key's, or isn't one this version implements, is
    refused. The token must also come from the production issuer, for this
    integration's audience, unexpired, and name a site, a profile and a device.
    With ``site_id``, its site must be that one. Claims it doesn't know are
    ignored.

    Raises ``TokenError``: ``wrong_site`` for another site's token, and
    ``invalid_token`` for anything else.
    """
    if not isinstance(token, str) or not token:
        raise TokenError(ERROR_INVALID_TOKEN)

    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError:
        raise TokenError(ERROR_INVALID_TOKEN) from None
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in SIGNING_ALGORITHMS:
        raise TokenError(ERROR_INVALID_TOKEN)

    kid = header.get("kid")
    key = next((k for k in keys if k.kid == kid and k.valid_at(now)), None)
    if key is None or key.alg != alg:
        raise TokenError(ERROR_INVALID_TOKEN)

    try:
        claims = jwt.decode(
            token,
            key.public_key,
            algorithms=[key.alg],
            audience=TOKEN_AUDIENCE,
            issuer=TOKEN_ISSUER,
            # Times are checked below, against the ``now`` given.
            options={
                "verify_exp": False,
                "verify_nbf": False,
                "verify_iat": False,
                "require": ["exp", "iss", "aud"],
            },
        )
    except jwt.PyJWTError:
        raise TokenError(ERROR_INVALID_TOKEN) from None

    exp = claims.get("exp")
    if not _is_number(exp) or exp + CLOCK_LEEWAY <= now:
        raise TokenError(ERROR_INVALID_TOKEN)
    nbf = claims.get("nbf")
    if nbf is not None and (not _is_number(nbf) or nbf - CLOCK_LEEWAY > now):
        raise TokenError(ERROR_INVALID_TOKEN)

    token_site_id = claims.get("siteId")
    profile_id = claims.get("profileId")
    device_id = claims.get("deviceId")
    if not all(
        isinstance(value, str) and value
        for value in (token_site_id, profile_id, device_id)
    ):
        raise TokenError(ERROR_INVALID_TOKEN)
    if site_id is not None and token_site_id != site_id:
        raise TokenError(ERROR_WRONG_SITE)

    scope = claims.get("scope")
    scopes = scope.split() if isinstance(scope, str) else []
    encryption_public_key = claims.get("encryptionPublicKey")
    return TokenClaims(
        site_id=token_site_id,
        profile_id=profile_id,
        device_id=device_id,
        admin=ADMIN_SCOPE in scopes,
        encryption_public_key=(
            encryption_public_key
            if isinstance(encryption_public_key, str) and encryption_public_key
            else None
        ),
    )


# ---------------------------------------------------------------------------
# The site link
# ---------------------------------------------------------------------------

# How long the link from a stored push token waits after its first attempt with
# no answer, in seconds. It doubles after each one after that, up to the longest.
LINK_RETRY_INTERVAL = 60
LINK_RETRY_LONGEST_INTERVAL = 3600


def link_retry_interval(failures: int) -> int:
    """Return how long to wait after this many attempts in a row with no answer."""
    return min(LINK_RETRY_INTERVAL * 2 ** (max(failures, 1) - 1), LINK_RETRY_LONGEST_INTERVAL)


def site_link(linked_site_id: str | None, site_id: str) -> str:
    """Return how the site linked now, if any, compares with ``site_id``.

    There's no unlink: a linked integration stays on its site until it's
    removed.
    """
    if linked_site_id is None:
        return LINK_NOT_LINKED
    if linked_site_id == site_id:
        return LINK_THIS_PROPERTY
    return LINK_ANOTHER_PROPERTY


def push_token_serial(push_token: Any) -> str | None:
    """Return the serial of a stored push token, its ``jti`` claim, or None.

    Read without verifying the token: the app only compares it with
    device-api's current serial. None when there's no token, it can't be read,
    or the claim isn't a non-empty string. A stored push token can be any
    string, since the v1 ``setup_push_notifications`` stores what it's given.
    """
    if not isinstance(push_token, str):
        return None
    try:
        claims = jwt.decode(push_token, options={"verify_signature": False})
    except (jwt.PyJWTError, ValueError):
        return None
    serial = claims.get("jti")
    return serial if isinstance(serial, str) and serial else None


# ---------------------------------------------------------------------------
# Encryption public keys
# ---------------------------------------------------------------------------

def parse_public_key(value: Any) -> ec.EllipticCurvePublicKey | None:
    """Read a P-256 public key from base64 of its DER SPKI, or return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        der = base64.b64decode(value, validate=True)
        public_key = serialization.load_der_public_key(der)
    except (binascii.Error, ValueError):
        return None
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP256R1
    ):
        return None
    return public_key


def token_public_key(claims: TokenClaims) -> ec.EllipticCurvePublicKey:
    """Return the encryption public key a checked token carries.

    Raises ``TokenError`` with ``public_key_required`` when it carries none, or
    one that isn't a P-256 key.
    """
    public_key = parse_public_key(claims.encryption_public_key)
    if public_key is None:
        raise TokenError(ERROR_PUBLIC_KEY_REQUIRED)
    return public_key


def choose_scheme(encryption: Any) -> str | None:
    """Return the scheme to use from a registration's ``encryption.schemes``.

    The first one the device lists that this version supports: schemes it
    doesn't know are skipped. A device that sends no ``encryption``, or no
    ``schemes``, gets ecies-p256, which every v2 device supports. None when
    there's nothing in common.
    """
    if encryption is None:
        return SCHEME_ECIES_P256
    if not isinstance(encryption, dict):
        return None
    schemes = encryption.get("schemes")
    if schemes is None:
        return SCHEME_ECIES_P256
    if not isinstance(schemes, list):
        return None
    return next(
        (scheme for scheme in schemes if isinstance(scheme, str) and scheme in SUPPORTED_SCHEMES),
        None,
    )


def public_key_id(public_key: ec.EllipticCurvePublicKey) -> str:
    """Return a public key's id, by the same rule as a signing key's ``kid``."""
    return compute_kid(
        public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )


# ---------------------------------------------------------------------------
# Registrations
# ---------------------------------------------------------------------------

def new_api_key() -> str:
    """Return a new API key: 32 random bytes, as base64url without padding."""
    return base64.urlsafe_b64encode(secrets.token_bytes(_KEY_BYTES)).rstrip(b"=").decode()


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def is_api_key(value: Any) -> bool:
    """Return whether ``value`` has an API key's form."""
    return isinstance(value, str) and _API_KEY_PATTERN.fullmatch(value) is not None


@dataclass(frozen=True)
class Registration:
    """A device registered with this integration."""

    profile_id: str
    device_id: str
    # SHA-256 hex of the API key. The key itself is never kept.
    api_key_hash: str
    # The device's encryption public key, from the token it registered with:
    # base64 of its DER SPKI.
    public_key: str
    # The public key's id (``public_key_id``).
    key_id: str
    # Whether the token the device registered with had the admin scope.
    admin: bool
    # Unix seconds.
    registered_at: float

    @property
    def scheme(self) -> str:
        return SCHEME_ECIES_P256

    def encryption_public_key(self) -> ec.EllipticCurvePublicKey:
        public_key = parse_public_key(self.public_key)
        if public_key is None:
            # Registrations are made and restored only with a valid key.
            raise ValueError("Not a P-256 public key")
        return public_key


class RegistrationError(Exception):
    """A registration that can't be made, with the error code to answer."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _device_key(profile_id: str, device_id: str) -> tuple[str, str]:
    return (profile_id, device_id)


def _restored_registration(value: Any) -> Registration | None:
    if not isinstance(value, dict):
        return None
    fields = (
        "profile_id",
        "device_id",
        "api_key_hash",
        "public_key",
        "key_id",
    )
    if not all(isinstance(value.get(field), str) and value.get(field) for field in fields):
        return None
    public_key = parse_public_key(value["public_key"])
    if public_key is None or public_key_id(public_key) != value["key_id"]:
        return None
    if not isinstance(value.get("admin"), bool) or not _is_number(value.get("registered_at")):
        return None
    return Registration(**{field: value[field] for field in fields}, admin=value["admin"],
                        registered_at=value["registered_at"])


class RegistrationBook:
    """Every device's registration, and the tombstones of revoked devices.

    A device has at most one registration. ``admin_registered`` turns on the
    first time a device registers with an admin-scoped token, and stays on.
    """

    def __init__(self) -> None:
        self._by_api_key_hash: dict[str, Registration] = {}
        self._by_device: dict[tuple[str, str], Registration] = {}
        self._tombstones: dict[tuple[str, str], float] = {}
        self.admin_registered = False

    # -- reading ------------------------------------------------------------

    def find_by_api_key(self, api_key: Any) -> Registration | None:
        if not is_api_key(api_key):
            return None
        return self._by_api_key_hash.get(hash_api_key(api_key))

    def find_by_device(self, profile_id: str, device_id: str) -> Registration | None:
        return self._by_device.get(_device_key(profile_id, device_id))

    def is_tombstoned(self, profile_id: str, device_id: str) -> bool:
        return _device_key(profile_id, device_id) in self._tombstones

    def registrations(self) -> list[Registration]:
        return list(self._by_device.values())

    # -- changing -----------------------------------------------------------

    def register(
        self,
        profile_id: str,
        device_id: str,
        *,
        admin: bool,
        public_key: str | None,
        now: float,
    ) -> tuple[str, Registration]:
        """Register the device, replacing any registration it had.

        ``public_key`` is the encryption public key from the device's token.
        Returns the new API key, which is kept only as its hash, and the
        registration. Raises ``RegistrationError``: ``device_revoked`` for a
        revoked device, ``public_key_required`` without a P-256 public key.
        """
        if self.is_tombstoned(profile_id, device_id):
            raise RegistrationError(ERROR_DEVICE_REVOKED)
        parsed_public_key = parse_public_key(public_key)
        if public_key is None or parsed_public_key is None:
            raise RegistrationError(ERROR_PUBLIC_KEY_REQUIRED)

        self._remove(profile_id, device_id)

        api_key = new_api_key()
        registration = Registration(
            profile_id=profile_id,
            device_id=device_id,
            api_key_hash=hash_api_key(api_key),
            public_key=public_key,
            key_id=public_key_id(parsed_public_key),
            admin=admin,
            registered_at=now,
        )
        self._add(registration)
        if admin:
            self.admin_registered = True
        return api_key, registration

    def revoke(self, profile_id: str, device_id: str, now: float) -> Registration | None:
        """Remove the device's registration and keep its tombstone.

        Returns the registration removed, or None if it had none. The
        tombstone is kept either way.
        """
        removed = self._remove(profile_id, device_id)
        self._tombstones.setdefault(_device_key(profile_id, device_id), now)
        return removed

    def _add(self, registration: Registration) -> None:
        self._by_api_key_hash[registration.api_key_hash] = registration
        self._by_device[_device_key(registration.profile_id, registration.device_id)] = (
            registration
        )

    def _remove(self, profile_id: str, device_id: str) -> Registration | None:
        registration = self._by_device.pop(_device_key(profile_id, device_id), None)
        if registration is not None:
            self._by_api_key_hash.pop(registration.api_key_hash, None)
        return registration

    # -- persistence ----------------------------------------------------------

    def to_data(self) -> dict[str, Any]:
        return {
            "registrations": [
                {
                    "profile_id": r.profile_id,
                    "device_id": r.device_id,
                    "api_key_hash": r.api_key_hash,
                    "public_key": r.public_key,
                    "key_id": r.key_id,
                    "admin": r.admin,
                    "registered_at": r.registered_at,
                }
                for r in self._by_device.values()
            ],
            "tombstones": [
                {"profile_id": profile_id, "device_id": device_id, "revoked_at": revoked_at}
                for (profile_id, device_id), revoked_at in self._tombstones.items()
            ],
            "admin_registered": self.admin_registered,
        }

    @classmethod
    def from_data(cls, data: Any) -> RegistrationBook:
        """Restore a book, leaving out any part that isn't valid."""
        book = cls()
        if not isinstance(data, dict):
            return book

        tombstones = data.get("tombstones")
        for item in tombstones if isinstance(tombstones, list) else []:
            if (
                isinstance(item, dict)
                and isinstance(item.get("profile_id"), str)
                and isinstance(item.get("device_id"), str)
                and _is_number(item.get("revoked_at"))
            ):
                book._tombstones[_device_key(item["profile_id"], item["device_id"])] = item[
                    "revoked_at"
                ]

        registrations = data.get("registrations")
        for item in registrations if isinstance(registrations, list) else []:
            registration = _restored_registration(item)
            if registration is not None and not book.is_tombstoned(
                registration.profile_id, registration.device_id
            ):
                book._add(registration)

        book.admin_registered = data.get("admin_registered") is True or any(
            registration.admin for registration in book.registrations()
        )
        return book


# ---------------------------------------------------------------------------
# Command envelopes
# ---------------------------------------------------------------------------

def _uncompressed_point(public_key: ec.EllipticCurvePublicKey) -> bytes:
    return public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


def _ecies_key(shared_secret: bytes, one_off_point: bytes, recipient_point: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_HKDF_INFO_PREFIX + one_off_point + recipient_point,
    ).derive(shared_secret)


def ecies_encrypt(
    recipient: ec.EllipticCurvePublicKey,
    aad: bytes,
    plaintext: bytes,
    *,
    one_off_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> str:
    """Encrypt to a P-256 public key with ecies-p256.

    A one-off key pair's ECDH with the recipient's key gives the shared secret,
    the 32-byte x-coordinate, and HKDF-SHA256 turns it into an AES-256-GCM key.
    Returns base64 of the one-off public key (65 bytes, uncompressed), the
    nonce (12), the ciphertext and the tag (16). ``one_off_key`` and ``nonce``
    are for tests: leave them out.
    """
    if one_off_key is None:
        one_off_key = ec.generate_private_key(ec.SECP256R1())
    if nonce is None:
        nonce = secrets.token_bytes(_NONCE_BYTES)
    one_off_point = _uncompressed_point(one_off_key.public_key())
    key = _ecies_key(
        one_off_key.exchange(ec.ECDH(), recipient),
        one_off_point,
        _uncompressed_point(recipient),
    )
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
    return base64.b64encode(one_off_point + nonce + ciphertext).decode()


def ecies_decrypt(recipient: ec.EllipticCurvePrivateKey, aad: bytes, data: str) -> bytes:
    """Decrypt ``ecies_encrypt``'s output with the recipient's private key.

    Raises ``ValueError`` when it can't be read, and
    ``cryptography.exceptions.InvalidTag`` when it doesn't match.
    """
    raw = base64.b64decode(data, validate=True)
    if len(raw) < _POINT_BYTES + _NONCE_BYTES + _TAG_BYTES:
        raise ValueError("Too short")
    one_off_point = raw[:_POINT_BYTES]
    nonce = raw[_POINT_BYTES : _POINT_BYTES + _NONCE_BYTES]
    ciphertext = raw[_POINT_BYTES + _NONCE_BYTES :]
    one_off_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), one_off_point)
    key = _ecies_key(
        recipient.exchange(ec.ECDH(), one_off_public),
        one_off_point,
        _uncompressed_point(recipient.public_key()),
    )
    return AESGCM(key).decrypt(nonce, ciphertext, aad)


def additional_data(
    profile_id: str, device_id: str, rpc_id: str, encryption: str, key_id: str | None
) -> bytes:
    """An envelope's additional data: its clear fields."""
    return "\n".join((profile_id, device_id, rpc_id, encryption, key_id or "")).encode()


def sealed_additional_data(profile_id: str, device_id: str, method: str) -> bytes:
    """A sealed object's additional data: the device it's for, and the method."""
    return f"sealed\n{profile_id}\n{device_id}\n{method}".encode()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()


def may_send_command(method: str, *, registered: bool) -> bool:
    """Return whether ``method`` can be sent to a device.

    A method in ``MUST_ENCRYPT_METHODS`` goes only to a registered device, in an
    encrypted envelope. A device that hasn't registered gets every other method
    as a ``bngnt_managed_device_command`` event.
    """
    return registered or method not in MUST_ENCRYPT_METHODS


class CommandRefusal(Enum):
    """Why a command isn't sent to a device, from ``command_refusal``."""

    NOT_LINKED = "not_linked"
    OFFLINE = "offline"
    APP_UPDATE_REQUIRED = "app_update_required"


def command_refusal(
    method: str, *, online: bool, registered: bool, linked: bool
) -> CommandRefusal | None:
    """Return why ``method`` isn't sent to a device, or None when it is.

    ``may_send_command`` decides whether the method can go to the device. When
    it can't, the app's version is the reason only once Home Assistant is
    linked: until then no device can register, whichever app it's on. That comes
    before the device being online, as bringing it online wouldn't help.
    """
    can_send = may_send_command(method, registered=registered)
    if not can_send and not linked:
        return CommandRefusal.NOT_LINKED
    if not online:
        return CommandRefusal.OFFLINE
    if not can_send:
        return CommandRefusal.APP_UPDATE_REQUIRED
    return None


def build_command_envelope(
    request: dict[str, Any],
    registration: Registration,
    *,
    one_off_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> dict[str, Any]:
    """Wrap a JSON-RPC request for a registered device.

    A method in ``MUST_ENCRYPT_METHODS`` is encrypted to the device's public
    key, and anything else goes in clear. A ``sealed`` object already in the
    request's params goes as it is.
    """
    rpc_id = request["id"]
    envelope: dict[str, Any] = {
        "profile_id": registration.profile_id,
        "device_id": registration.device_id,
        "id": rpc_id,
    }

    if request.get("method") not in MUST_ENCRYPT_METHODS:
        envelope["encryption"] = SCHEME_NONE
        envelope["payload"] = request
        return envelope

    aad = additional_data(
        registration.profile_id,
        registration.device_id,
        rpc_id,
        registration.scheme,
        registration.key_id,
    )
    envelope["encryption"] = registration.scheme
    envelope["key_id"] = registration.key_id
    envelope["payload"] = ecies_encrypt(
        registration.encryption_public_key(),
        aad,
        _json_bytes(request),
        one_off_key=one_off_key,
        nonce=nonce,
    )
    return envelope


def seal_to(
    fields: dict[str, Any],
    public_key: ec.EllipticCurvePublicKey,
    profile_id: str,
    device_id: str,
    method: str,
    *,
    one_off_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> dict[str, str]:
    """Seal ``fields`` to ``public_key``, for the device ``profile_id`` +
    ``device_id`` and for ``method``.

    Returns ``{"scheme": "ecies-p256", "data": ...}``, for the message's
    ``sealed`` field. The device opens it and adds the fields back.
    """
    return {
        "scheme": SCHEME_ECIES_P256,
        "data": ecies_encrypt(
            public_key,
            sealed_additional_data(profile_id, device_id, method),
            _json_bytes(fields),
            one_off_key=one_off_key,
            nonce=nonce,
        ),
    }


def seal(
    fields: dict[str, Any],
    registration: Registration,
    method: str,
    *,
    one_off_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> dict[str, str]:
    """Seal ``fields`` to a registered device, for ``method``.

    Returns ``{"scheme": "ecies-p256", "data": ...}``, for the message's
    ``sealed`` field. The device opens it and adds the fields back.
    """
    return seal_to(
        fields,
        registration.encryption_public_key(),
        registration.profile_id,
        registration.device_id,
        method,
        one_off_key=one_off_key,
        nonce=nonce,
    )


def admin_code_lookup_for_attached_device(
    lookup: dict[str, Any],
    book: RegistrationBook,
    profile_id: str,
    device_id: str,
) -> dict[str, Any]:
    """Return what an admin's lookup answers for a record's stored ``lookup``.

    ``profile_id`` and ``device_id`` are the device attached to the record, not
    the caller. The integration only hands out codes for devices registered
    with it: for any other, the answer reads as if the device had never
    reported, whatever the store holds.
    """
    if book.find_by_device(profile_id, device_id) is not None:
        return lookup
    return dict(UNKNOWN_ADMIN_CODE_LOOKUP)


def seal_settings_restore(
    result: dict[str, Any],
    registration: Registration,
    *,
    one_off_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> dict[str, Any]:
    """Move a settings restore answer's ``admin_code`` into a sealed object.

    An answer with no restore is returned as it is. A restore always gets its
    sealed object, holding ``admin_code`` whether it's a code or null.
    """
    restore = result.get("restore")
    if not isinstance(restore, dict):
        return result
    restore = dict(restore)
    admin_code = restore.pop("admin_code", None)
    restore["sealed"] = seal(
        {"admin_code": admin_code},
        registration,
        SEALED_METHOD_SETTINGS_RESTORE,
        one_off_key=one_off_key,
        nonce=nonce,
    )
    return {**result, "restore": restore}


def seal_admin_code_answer(
    result: dict[str, Any],
    claims: TokenClaims,
    *,
    one_off_key: ec.EllipticCurvePrivateKey | None = None,
    nonce: bytes | None = None,
) -> dict[str, Any]:
    """Move an admin code answer's ``admin_code`` into a sealed object.

    Sealed to the encryption public key in the caller's checked token, for the
    caller's own profile and device, which opens it. The answer always gets its
    sealed object, holding ``admin_code`` whether it's a code or null. Raises
    ``TokenError`` with ``public_key_required`` when the token carries no
    P-256 public key.
    """
    public_key = token_public_key(claims)
    answer = dict(result)
    admin_code = answer.pop("admin_code", None)
    answer["sealed"] = seal_to(
        {"admin_code": admin_code},
        public_key,
        claims.profile_id,
        claims.device_id,
        SEALED_METHOD_ADMIN_CODE_GET,
        one_off_key=one_off_key,
        nonce=nonce,
    )
    return answer


# ---------------------------------------------------------------------------
# Site config encryption key
# ---------------------------------------------------------------------------

# States of the helper that aren't a key.
_HELPER_STATES_WITHOUT_A_KEY = frozenset({"unknown", "unavailable"})


def restored_encryption_key(data: Any) -> str | None:
    """Return the key in what the store loaded, or None when it has none."""
    key = data.get("key") if isinstance(data, dict) else None
    return key if isinstance(key, str) and key else None


def helper_encryption_key(state: Any) -> str | None:
    """Return the key in the helper's state, or None when it holds none."""
    if not isinstance(state, str) or not state or state in _HELPER_STATES_WITHOUT_A_KEY:
        return None
    return state


def held_encryption_key(
    stored: str | None, read_helper_state: Callable[[], Any]
) -> str | None:
    """Return the key held: the stored one, otherwise the helper's, otherwise None.

    ``read_helper_state`` is called only when no key is stored.
    """
    if stored is not None:
        return stored
    return helper_encryption_key(read_helper_state())


def _sealed_encryption_key_answer(
    key: str, public_key: ec.EllipticCurvePublicKey, profile_id: str, device_id: str
) -> dict[str, Any]:
    return {
        "success": True,
        "sealed": seal_to(
            {"key": key}, public_key, profile_id, device_id, SEALED_METHOD_ENCRYPTION_KEY_GET
        ),
    }


def encryption_key_answer(key: str | None, registration: Registration) -> dict[str, Any]:
    """Answer a registered device's read of the key held, or of None for none.

    The key comes sealed to the registration's public key. With none held, the
    answer is ``key: null``, in clear.
    """
    if key is None:
        return {"success": True, "key": None}
    return _sealed_encryption_key_answer(
        key,
        registration.encryption_public_key(),
        registration.profile_id,
        registration.device_id,
    )


def claimed_encryption_key_answer(key: str, claims: TokenClaims) -> dict[str, Any]:
    """Answer an admin's claim with the key kept, sealed to the key in its token.

    Raises ``TokenError`` with ``public_key_required`` when the token carries no
    P-256 public key.
    """
    return _sealed_encryption_key_answer(
        key, token_public_key(claims), claims.profile_id, claims.device_id
    )


# ---------------------------------------------------------------------------
# Token labels
# ---------------------------------------------------------------------------

def token_label(client_name: Any) -> dict[str, Any] | None:
    """Return the ids in a long-lived token's ``bngnt1:`` label, if it has one."""
    if not isinstance(client_name, str):
        return None
    start = client_name.find(TOKEN_LABEL_PREFIX)
    if start < 0:
        return None
    try:
        data = json.loads(client_name[start + len(TOKEN_LABEL_PREFIX) :])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def is_device_token_label(
    client_name: Any, site_id: str, profile_id: str, device_id: str
) -> bool:
    """Return whether a token's label names this device of this site."""
    label = token_label(client_name)
    return (
        label is not None
        and label.get("s") == site_id
        and label.get("p") == profile_id
        and label.get("d") == device_id
    )
