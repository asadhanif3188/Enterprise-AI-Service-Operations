"""Local, test-only opaque credential verification (ADR-0005).

What this module does:

* Generates opaque credentials with ``secrets`` (256 bits of randomness).
* Keeps a server-controlled credential store: a local JSON file outside
  version control that holds only SHA-256 digests mapped to ``actor.id``,
  never raw credentials.
* Resolves a presented credential to a verified ``actor.id``, or raises a
  typed outcome carrying the status ADR-0005 assigns to it.

What it does not do: authorization. A verified actor ID says who is calling,
not what they may do; memberships, roles, and the target organization are
not consulted here. There is no HTTP layer: ``status_equivalent`` documents
the ADR-0005 mapping for a future boundary and is not an HTTP response.

Not production identity management: no OIDC/SSO, expiry, rate limiting, or
audit trail, and anyone who can edit the store file or the database can
change who a credential resolves to.

Raw credential values and their digests are never logged or placed in
exception messages. Logs carry only the outcome category, the store's
non-secret ``credential_id``, and the verified ``actor_id``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NoReturn, Protocol
from uuid import UUID

logger = logging.getLogger("eaiso.auth")

CREDENTIAL_PREFIX = "eaiso_test_"
CREDENTIAL_RANDOM_BYTES = 32  # 256 bits
# token_urlsafe(32) yields 43 unpadded base64url characters.
_CREDENTIAL_RE = re.compile(r"eaiso_test_[A-Za-z0-9_-]{43}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CREDENTIAL_ID_RE = re.compile(r"[0-9a-f]{16}")

STORE_VERSION = 1
DEFAULT_STORE_PATH = Path(".local") / "test-credentials.json"
STORE_PATH_ENV = "EAISO_CREDENTIALS_FILE"

ACTIVE = "active"
REVOKED = "revoked"
_ENTRY_KEYS = frozenset({"credential_id", "digest", "actor_id", "status", "created_at", "revoked_at"})


# ----- Outcomes ---------------------------------------------------------------

class AuthenticationError(Exception):
    """Base class. ``status_equivalent`` is the ADR-0005 status; not an HTTP response."""

    status_equivalent = 500

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Unauthenticated(AuthenticationError):
    """401 equivalent: missing, malformed, unknown, or revoked credential, or no actor row."""

    status_equivalent = 401
    REASONS = frozenset({"missing", "malformed", "unknown", "revoked", "actor_not_found"})


class InactiveActor(AuthenticationError):
    """403 equivalent: the credential is valid but the actor is deactivated."""

    status_equivalent = 403

    def __init__(self) -> None:
        super().__init__("inactive_actor")


class AuthenticationUnavailable(AuthenticationError):
    """Generic dependency failure (configuration or database). Never a success."""

    status_equivalent = 500


class CredentialConfigurationError(Exception):
    """The credential store is missing, unreadable, or invalid. Messages hold no secrets."""


@dataclass(frozen=True)
class VerifiedActor:
    actor_id: UUID
    credential_id: str


# ----- Credential values ---------------------------------------------------------

def generate_credential() -> str:
    """Return a new opaque credential: prefix plus 256 random bits, base64url."""
    return CREDENTIAL_PREFIX + secrets.token_urlsafe(CREDENTIAL_RANDOM_BYTES)


def is_well_formed(value: object) -> bool:
    if not isinstance(value, str) or _CREDENTIAL_RE.fullmatch(value) is None:
        return False
    # Reject non-canonical encodings (trailing bits set) so one secret has one form.
    body = value[len(CREDENTIAL_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=")
    except ValueError:
        return False
    return len(raw) == CREDENTIAL_RANDOM_BYTES and \
        base64.urlsafe_b64encode(raw).rstrip(b"=").decode() == body


def credential_digest(credential: str) -> str:
    """Non-reversible lookup key. A fast hash suffices: the input has 256 bits of entropy."""
    return "sha256:" + hashlib.sha256(credential.encode("ascii")).hexdigest()


# ----- Store -------------------------------------------------------------------

@dataclass(frozen=True)
class CredentialEntry:
    credential_id: str
    digest: str
    actor_id: UUID
    status: str
    created_at: str
    revoked_at: str | None = None

    def to_json(self) -> dict:
        data = {"credential_id": self.credential_id, "digest": self.digest,
                "actor_id": str(self.actor_id), "status": self.status,
                "created_at": self.created_at}
        if self.revoked_at is not None:
            data["revoked_at"] = self.revoked_at
        return data


def _parse_entry(index: int, item: object) -> CredentialEntry:
    where = f"credentials[{index}]"
    if not isinstance(item, dict):
        raise CredentialConfigurationError(f"{where} must be an object")
    unknown = set(item) - _ENTRY_KEYS
    if unknown:
        raise CredentialConfigurationError(f"{where} has unsupported keys: {sorted(unknown)}")
    for key in ("credential_id", "digest", "actor_id", "status", "created_at"):
        if not isinstance(item.get(key), str):
            raise CredentialConfigurationError(f"{where}.{key} must be a string")
    if _CREDENTIAL_ID_RE.fullmatch(item["credential_id"]) is None:
        raise CredentialConfigurationError(f"{where}.credential_id is malformed")
    if _DIGEST_RE.fullmatch(item["digest"]) is None:
        raise CredentialConfigurationError(f"{where}.digest is malformed")
    try:
        actor_id = UUID(item["actor_id"])
    except ValueError:
        raise CredentialConfigurationError(f"{where}.actor_id is not a UUID") from None
    if item["status"] not in (ACTIVE, REVOKED):
        raise CredentialConfigurationError(f"{where}.status must be 'active' or 'revoked'")
    revoked_at = item.get("revoked_at")
    if revoked_at is not None and not isinstance(revoked_at, str):
        raise CredentialConfigurationError(f"{where}.revoked_at must be a string")
    return CredentialEntry(item["credential_id"], item["digest"], actor_id,
                           item["status"], item["created_at"], revoked_at)


def parse_store(text: str) -> tuple[CredentialEntry, ...]:
    try:
        data = json.loads(text)
    except ValueError:
        raise CredentialConfigurationError("credential store is not valid JSON") from None
    if not isinstance(data, dict) or set(data) != {"version", "credentials"}:
        raise CredentialConfigurationError("credential store must have exactly 'version' and 'credentials'")
    if data["version"] != STORE_VERSION:
        raise CredentialConfigurationError(f"unsupported credential store version (expected {STORE_VERSION})")
    if not isinstance(data["credentials"], list):
        raise CredentialConfigurationError("'credentials' must be a list")
    entries = tuple(_parse_entry(i, item) for i, item in enumerate(data["credentials"]))
    for attr in ("digest", "credential_id"):
        values = [getattr(e, attr) for e in entries]
        if len(values) != len(set(values)):
            raise CredentialConfigurationError(f"duplicate {attr} in credential store")
    return entries


def read_store(path: Path) -> tuple[CredentialEntry, ...]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        raise CredentialConfigurationError("credential store cannot be read") from None
    return parse_store(text)


def write_store(path: Path, entries: tuple[CredentialEntry, ...] | list[CredentialEntry]) -> None:
    """Validate, then atomically replace the store (temporary file plus os.replace)."""
    path = Path(path)
    text = json.dumps({"version": STORE_VERSION,
                       "credentials": [e.to_json() for e in entries]}, indent=2) + "\n"
    parse_store(text)  # never write a store the resolver would reject
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)  # owner-only on POSIX; largely ignored on Windows
        _replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(tmp: str, path: Path, attempts: int = 25) -> None:
    """os.replace, retrying briefly on Windows sharing violations.

    Windows refuses to replace a file another process (a reader, an indexer,
    or antivirus) holds open, raising PermissionError. The retry is bounded;
    the last error propagates and the previous store stays in place.
    """
    for attempt in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02)


LOCK_TIMEOUT_S = 5.0


@contextmanager
def _writer_lock(path: Path, timeout_s: float | None = None):
    """Exclusive lock file so concurrent operator runs cannot lose updates.

    Readers are unaffected (they see the old or new file, never a mix). A lock
    left behind by a killed process must be removed by the operator.
    """
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + (LOCK_TIMEOUT_S if timeout_s is None else timeout_s)
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except (FileExistsError, PermissionError):
            # Windows reports a lock file that another writer is deleting as
            # PermissionError rather than FileExistsError; both mean "busy".
            if time.monotonic() >= deadline:
                raise CredentialConfigurationError(
                    f"credential store is locked by another writer, or its directory is not "
                    f"writable ({lock.name}); if no other run is active, remove the lock file"
                ) from None
            time.sleep(0.02)
    try:
        os.close(fd)
        yield
    finally:
        try:
            os.unlink(lock)
        except OSError:
            pass


def store_path_from_env() -> Path:
    return Path(os.environ.get(STORE_PATH_ENV) or DEFAULT_STORE_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def issue_credential(path: Path, actor_id: UUID) -> tuple[str, CredentialEntry]:
    """Create a new active credential for ``actor_id``. Returns the raw value once."""
    with _writer_lock(path):
        entries = list(read_store(path)) if Path(path).exists() else []
        credential = generate_credential()
        entry = CredentialEntry(secrets.token_hex(8), credential_digest(credential),
                                UUID(str(actor_id)), ACTIVE, _now())
        write_store(path, entries + [entry])
    return credential, entry


def _replace(entries, credential_id: str, new_entry: CredentialEntry | None):
    found = False
    out = []
    for e in entries:
        if e.credential_id == credential_id:
            found = True
            if new_entry is not None:
                out.append(new_entry)
        else:
            out.append(e)
    if not found:
        raise CredentialConfigurationError("credential_id not found in store")
    return out


def revoke_credential(path: Path, credential_id: str) -> CredentialEntry:
    with _writer_lock(path):
        entries = read_store(path)
        current = next((e for e in entries if e.credential_id == credential_id), None)
        if current is None:
            raise CredentialConfigurationError("credential_id not found in store")
        revoked = CredentialEntry(current.credential_id, current.digest, current.actor_id,
                                  REVOKED, current.created_at, current.revoked_at or _now())
        write_store(path, _replace(entries, credential_id, revoked))
    return revoked


def remove_credential(path: Path, credential_id: str) -> None:
    with _writer_lock(path):
        write_store(path, _replace(read_store(path), credential_id, None))


def rotate_credential(path: Path, credential_id: str) -> tuple[str, CredentialEntry]:
    """Issue a replacement for the same actor and revoke the old one in one write."""
    with _writer_lock(path):
        entries = read_store(path)
        old = next((e for e in entries if e.credential_id == credential_id), None)
        if old is None:
            raise CredentialConfigurationError("credential_id not found in store")
        if old.status != ACTIVE:
            raise CredentialConfigurationError("only an active credential can be rotated")
        credential = generate_credential()
        new = CredentialEntry(secrets.token_hex(8), credential_digest(credential), old.actor_id,
                              ACTIVE, _now())
        revoked = CredentialEntry(old.credential_id, old.digest, old.actor_id, REVOKED,
                                  old.created_at, _now())
        write_store(path, _replace(entries, credential_id, revoked) + [new])
    return credential, new


class FileCredentialStore:
    """Re-reads the store file on every lookup and re-parses it when its content changes.

    Revocation and rotation therefore take effect for the next resolution
    without a restart. Change detection uses a content hash, not timestamps,
    so rapid successive writes cannot be missed. Any read or validation
    failure fails closed; a previously loaded mapping is never used after the
    file becomes unreadable or invalid.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._content_hash: bytes | None = None
        self._by_digest: dict[str, CredentialEntry] = {}
        self._lock = threading.Lock()
        self.load()  # fail fast on startup

    def load(self) -> None:
        try:
            raw = self.path.read_bytes()
        except OSError:
            self._content_hash = None
            raise CredentialConfigurationError("credential store cannot be read") from None
        content_hash = hashlib.sha256(raw).digest()
        if content_hash == self._content_hash:
            return
        self._content_hash = None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialConfigurationError("credential store is not UTF-8") from None
        entries = parse_store(text)
        self._by_digest = {e.digest: e for e in entries}
        self._content_hash = content_hash

    def lookup(self, digest: str) -> CredentialEntry | None:
        with self._lock:  # reload and read as one step for concurrent callers
            self.load()
            return self._by_digest.get(digest)


# ----- Actor status lookup --------------------------------------------------------

class ActorStatusLookup(Protocol):
    def __call__(self, actor_id: UUID) -> bool | None:
        """Return the actor's is_active flag, or None if no actor row exists."""


class PostgresActorLookup:
    """One read-only statement per resolution, with bounded connect and statement time."""

    def __init__(self, dsn: str, *, connect_timeout_s: int = 5, statement_timeout_ms: int = 5000):
        self.dsn = dsn
        self.connect_timeout_s = connect_timeout_s
        self.statement_timeout_ms = statement_timeout_ms

    def __call__(self, actor_id: UUID) -> bool | None:
        import psycopg

        with psycopg.connect(self.dsn, autocommit=True, connect_timeout=self.connect_timeout_s,
                             options=f"-c statement_timeout={int(self.statement_timeout_ms)}") as conn:
            row = conn.execute("SELECT is_active FROM actor WHERE id = %s", (actor_id,)).fetchone()
        return None if row is None else bool(row[0])


# ----- Resolver -----------------------------------------------------------------

class CredentialResolver:
    """Resolve an opaque credential to a verified actor. Identity only, not authorization.

    The only input is the presented credential. There is deliberately no way to
    pass an actor ID, display name, organization ID, or header as identity.
    """

    def __init__(self, store: FileCredentialStore, actor_lookup: Callable[[UUID], bool | None]):
        self._store = store
        self._actor_lookup = actor_lookup

    def resolve(self, credential: object) -> VerifiedActor:
        if credential is None or credential == "":
            self._deny("missing")
        # Only an exact str: a subclass could pass the format check on its own
        # buffer and override encode() to redirect the digest to another credential.
        if type(credential) is not str or not is_well_formed(credential):
            self._deny("malformed")
        assert isinstance(credential, str)
        digest = credential_digest(credential)
        del credential  # nothing below needs the raw value

        # Failures are recorded inside the except blocks and raised after them, so
        # the dependency exception is neither the __cause__ nor the __context__ of
        # the authentication error and cannot leak through exception chaining.
        store_failure: str | None = None
        entry: CredentialEntry | None = None
        try:
            entry = self._store.lookup(digest)
        except CredentialConfigurationError:
            store_failure = "configuration"
        except Exception:  # noqa: BLE001 - fail closed, without echoing details
            store_failure = "store_error"
        if store_failure is not None:
            logger.error("auth.unavailable reason=%s", store_failure)
            raise AuthenticationUnavailable("configuration")

        if entry is None:
            self._deny("unknown")
        if entry.status != ACTIVE:
            self._deny("revoked", entry.credential_id)

        lookup_error: str | None = None
        is_active: bool | None = None
        try:
            is_active = self._actor_lookup(entry.actor_id)
        except Exception as exc:  # noqa: BLE001 - any dependency failure fails closed
            lookup_error = type(exc).__name__
        if lookup_error is not None:
            logger.error("auth.unavailable reason=database error_type=%s credential_id=%s",
                         lookup_error, entry.credential_id)
            raise AuthenticationUnavailable("database")

        if is_active is None:
            self._deny("actor_not_found", entry.credential_id)
        if is_active is not True:
            logger.info("auth.denied reason=inactive_actor credential_id=%s actor_id=%s",
                        entry.credential_id, entry.actor_id)
            raise InactiveActor()
        logger.info("auth.verified credential_id=%s actor_id=%s",
                    entry.credential_id, entry.actor_id)
        return VerifiedActor(entry.actor_id, entry.credential_id)

    @staticmethod
    def _deny(reason: str, credential_id: str | None = None) -> NoReturn:
        if credential_id is None:
            logger.info("auth.denied reason=%s", reason)
        else:
            logger.info("auth.denied reason=%s credential_id=%s", reason, credential_id)
        raise Unauthenticated(reason)


def resolver_from_environment(dsn: str, store_path: Path | None = None) -> CredentialResolver:
    """Build a resolver from server-side configuration; fails closed if it is invalid."""
    return CredentialResolver(FileCredentialStore(store_path or store_path_from_env()),
                              PostgresActorLookup(dsn))
