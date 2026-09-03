"""
Twitch OAuth Credentials

One module, one record shape, for the on-disk Twitch token file that
stream-monitoring and the Flink clip-detector job both read and write.

Duplicated at services/stream-monitoring/token_manager.py -- the two build
contexts are per-service, so this file cannot be a real shared import. Keep
both copies identical.
"""

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import requests

logger = logging.getLogger("token_manager")

TWITCH_TOKEN_ENDPOINT = "https://id.twitch.tv/oauth2/token"

# stream-monitoring and the Flink containers all run as uid:gid 9999 and all
# write this file. mkstemp() creates its temp file at 0600 owned by the
# writer; chowning every write to the shared group keeps it readable to the
# others even if the uids ever diverge again. Defaults to 9999, the Flink
# base image's baked-in gid (and what stream-monitoring's Dockerfile now
# matches) -- override via env if that ever stops matching.
#
# The temp file is created *in* secrets/, so that directory must be writable
# by gid 9999 (drwxrwxr-x). A host re-seed run outside the group can drop the
# group-write bit; seed_twitch_tokens.py restores it, and every refresh below
# degrades to in-memory-only rather than failing if it is missing.
TWITCH_TOKEN_GID = int(os.environ.get("TWITCH_TOKEN_GID", "9999"))


@dataclass(frozen=True)
class TokenRecord:
    access_token: str
    refresh_token: str
    scopes: list[str]
    created_at: Optional[str]
    updated_at: Optional[str]


class TwitchCredentials:
    """Owns the on-disk Twitch OAuth token record.

    Three guarantees this class holds so callers don't have to:
    one record shape (scopes/created_at survive every write), atomic writes
    (a reader never sees a torn file), a cross-process lock across
    read-refresh-write (two containers refreshing at once can't leave either
    holding a dead token -- Twitch rotates the refresh token on use), and
    cross-container read access (every write lands group-readable by
    TWITCH_TOKEN_GID, regardless of which container's uid wrote it).
    """

    def __init__(self, token_file: Path):
        self.token_file = Path(token_file)

    def load(self) -> TokenRecord:
        """Read the current token record. Takes no lock: atomic writes mean
        a concurrent write is always either fully visible or not yet
        visible, never torn."""
        return self._record_from_dict(self._read_raw())

    def persist(self, access_token: str, refresh_token: str) -> TokenRecord:
        """Store a new access/refresh token pair.

        Always reads the file first and keeps its `scopes` and `created_at`,
        so a caller that never called `load` can't blank them out."""
        with self._locked():
            existing = self._read_raw()
            record = TokenRecord(
                access_token=access_token,
                refresh_token=refresh_token,
                scopes=existing.get("scopes", []),
                created_at=existing.get("created_at"),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            self._write_atomic(record)
            return record

    def refresh(self, client_id: str, client_secret: str) -> TokenRecord:
        """Refresh against Twitch's token endpoint and persist the result.

        Holds the file lock for the whole read-refresh-write sequence, so a
        refresh racing from the other container waits instead of both
        rotating the refresh token and one of them ending up with a dead
        one.
        """
        with self._locked():
            existing = self._read_raw()
            current_refresh_token = existing.get("refresh_token")
            if not current_refresh_token:
                raise ValueError(f"Token file missing refresh_token: {self.token_file}")

            logger.info("Refreshing Twitch access token")
            response = requests.post(
                TWITCH_TOKEN_ENDPOINT,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": current_refresh_token,
                },
                timeout=30,
            )
            if response.status_code != 200:
                logger.error(
                    "Token refresh failed",
                    extra={"status_code": response.status_code, "body": response.text},
                )
                response.raise_for_status()
            data = response.json()

            record = TokenRecord(
                access_token=data["access_token"],
                # Twitch omits refresh_token in the response when it chose not
                # to rotate it -- keep the one already on file in that case.
                refresh_token=data.get("refresh_token", current_refresh_token),
                scopes=existing.get("scopes", []),
                created_at=existing.get("created_at"),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            try:
                self._write_atomic(record)
            except OSError as exc:
                # The refresh succeeded -- `record` holds a live access token.
                # Failing to persist it (secrets/ not group-writable, disk
                # full) must not turn a working refresh into a hard clip
                # failure: hand the token back so the caller keeps going. The
                # costs -- another process won't see this token, and a rotated
                # refresh_token is lost on restart -- are worth an ERROR, not
                # an exception.
                logger.error(
                    "Twitch token refreshed but could not be persisted to "
                    "%s: %s -- using it in memory only",
                    self.token_file,
                    exc,
                )
            logger.info(
                "Token refreshed successfully",
                extra={"expires_in": data.get("expires_in", "unknown")},
            )
            return record

    # -- internals --

    def _read_raw(self) -> dict:
        try:
            with open(self.token_file, "r") as f:
                return json.load(f)
        except FileNotFoundError:
            raise FileNotFoundError(
                f"Token file not found: {self.token_file}\n"
                "Run 'python seed_twitch_tokens.py' to generate tokens."
            )

    def _record_from_dict(self, data: dict) -> TokenRecord:
        access_token = data.get("access_token")
        refresh_token = data.get("refresh_token")
        if not access_token:
            raise ValueError(f"Token file missing access_token: {self.token_file}")
        if not refresh_token:
            raise ValueError(f"Token file missing refresh_token: {self.token_file}")
        return TokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=data.get("scopes", []),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )

    def _write_atomic(self, record: TokenRecord) -> None:
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=self.token_file.parent, prefix=".tmp-tokens-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {
                        "access_token": record.access_token,
                        "refresh_token": record.refresh_token,
                        "scopes": record.scopes,
                        "created_at": record.created_at,
                        "updated_at": record.updated_at,
                    },
                    f,
                    indent=2,
                )
            try:
                os.chown(tmp_path, -1, TWITCH_TOKEN_GID)
            except PermissionError:
                # Only the containers (uid 9999, already in this group) ever
                # need this to succeed. An unprivileged local run -- e.g. the
                # host venv this test suite normally runs under -- isn't part
                # of any cross-container race, so it's fine for the file to
                # keep the process's own default gid.
                logger.warning(
                    "Could not chown %s to gid %d; not running with the "
                    "privilege this needs outside a container",
                    tmp_path, TWITCH_TOKEN_GID,
                )
            os.chmod(tmp_path, 0o640)
            os.replace(tmp_path, self.token_file)
        except BaseException:
            os.unlink(tmp_path)
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Advisory exclusive lock, held across read-refresh-write.

        Locks a stable sidecar path rather than the token file itself: a
        flock is tied to the inode it was opened on, and persist/refresh
        replace the token file's inode on every write. Locking the token
        file directly would let a writer that starts right after a replace
        acquire the lock on the *new* inode while the previous holder's
        write is still finishing on the old one -- two writers proceeding
        at once despite one supposedly waiting on the other. All three
        containers mount the same host secrets/ directory, so the sidecar
        is visible across processes the same way the token file is.

        The three containers now all run as uid:gid 9999, but a plain
        open(path, "w") still applies the process umask, so the first
        container to create the sidecar could leave it unreadable to the
        others (or to a host re-seed). os.open with an explicit mode plus an
        fchmod forces the sidecar to stay 0o666 regardless of umask or
        writer -- cheap insurance against the uids diverging again.
        """
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.token_file.with_name(self.token_file.name + ".lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            os.fchmod(fd, 0o666)
        except PermissionError:
            pass  # sidecar already exists and is owned by another container's user
        with os.fdopen(fd, "r+") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


_credentials: Optional[TwitchCredentials] = None


def get_credentials() -> TwitchCredentials:
    """Get or create the global TwitchCredentials instance, reading the
    token file path from the required TWITCH_TOKEN_FILE environment
    variable."""
    global _credentials
    if _credentials is None:
        _credentials = TwitchCredentials(Path(os.environ["TWITCH_TOKEN_FILE"]))
    return _credentials
