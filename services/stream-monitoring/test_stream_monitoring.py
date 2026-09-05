#!/usr/bin/env python3
"""
Unit tests for Stream Monitoring Service

Tests token management, message processing, and service components.
"""

import asyncio
import importlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from psycopg2.extensions import adapt
from psycopg2.extras import execute_values
from redis.exceptions import ResponseError

from desired_set_store import (
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
    DesiredSet,
    RedisDesiredSetStore,
)
import eventsub_pool
import reconciler as reconciler_module
import stream_monitoring_service
import token_manager
from eventsub_pool import (
    SUBSCRIPTIONS_PER_CONNECTION,
    EventSubPoolTransport,
    map_chat_message,
    to_epoch_ms,
)
from reconciler import (
    REFUSAL_RECHECK_DAYS,
    PostgresRefusalStore,
    RateLimitedError,
    Reconciler,
    ReconcilerConfig,
    RefusalStore,
    StubTransport,
    SubscriptionRefusedError,
    TransientSessionError,
    TransportError,
)
from stream_monitoring_service import StreamMonitoringService, compute_desired_set
from test_support import FakeRedis
from token_manager import TokenRecord, TwitchCredentials


def make_stream(login, user_id):
    """A stand-in for one Helix stream row."""
    stream = MagicMock()
    stream.user_login = login
    stream.user_id = str(user_id)
    return stream


class FakeTwitch:
    """Serves a fixed ranking through the auto-paginating get_streams API."""

    def __init__(self, streams, error=None):
        self.streams = streams
        self.error = error

    def get_streams(self, first=100):
        async def pages():
            if self.error is not None:
                raise self.error
            for stream in self.streams:
                yield stream

        return pages()


class CountingCursor:
    """Cursor proxy compatible with the real psycopg2 execute_values helper."""

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def mogrify(self, template, params=None):
        template_bytes = (
            template.encode(self.connection.encoding)
            if isinstance(template, str)
            else template
        )
        if params is None:
            return template_bytes
        quoted = tuple(adapt(value).getquoted() for value in params)
        rendered = template_bytes % quoted
        self.connection.mogrified_rows.append(rendered)
        return rendered

    def execute(self, query, params=None):
        query_bytes = (
            query.encode(self.connection.encoding)
            if isinstance(query, str)
            else bytes(query)
        )
        self.connection.execute_count += 1
        self.connection.executed_sql.append(query_bytes)
        self.connection.executed_params.append(params)
        if self.connection.execute_error is not None:
            raise self.connection.execute_error

    def fetchall(self):
        return list(self.connection.fetchall_rows)

    def fetchone(self):
        return (
            self.connection.fetchone_rows.pop(0)
            if self.connection.fetchone_rows
            else None
        )


class CountingConnection:
    encoding = "UTF8"

    def __init__(self, *, execute_error=None, commit_error=None, rollback_error=None):
        self.execute_error = execute_error
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.fetchall_rows = []
        self.fetchone_rows = []
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.executed_sql = []
        self.executed_params = []
        self.mogrified_rows = []

    def cursor(self):
        return CountingCursor(self)

    def commit(self):
        self.commit_count += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self):
        self.rollback_count += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def reset_measured(self):
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.executed_sql.clear()
        self.executed_params.clear()
        self.mogrified_rows.clear()


class CountingPool:
    """One-connection pool proxy with explicit acquisition/return counts."""

    def __init__(self, connection=None, *, getconn_error=None):
        self.connection = connection or CountingConnection()
        self.getconn_error = getconn_error
        self.getconn_count = 0
        self.putconn_count = 0
        self.discard_count = 0
        self.returned_connections = []

    def getconn(self):
        self.getconn_count += 1
        if self.getconn_error is not None:
            raise self.getconn_error
        return self.connection

    def putconn(self, connection, close=False):
        self.putconn_count += 1
        self.discard_count += int(close)
        self.returned_connections.append((connection, close))

    def reset_measured(self):
        self.getconn_count = 0
        self.putconn_count = 0
        self.discard_count = 0
        self.returned_connections.clear()
        self.connection.reset_measured()


class ProductionCallCountingCursor:
    """Count production cursor calls while delegating to real Postgres."""

    def __init__(self, connection, cursor):
        self.connection = connection
        self._cursor = cursor

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._cursor.__exit__(exc_type, exc, traceback)

    def mogrify(self, query, params=None):
        return self._cursor.mogrify(query, params)

    def execute(self, query, params=None):
        self.connection.execute_count += 1
        self.connection.executed_sql.append(query)
        self.connection.executed_params.append(params)
        return self._cursor.execute(query, params)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()


class ProductionCallCountingConnection:
    def __init__(self, connection):
        self._connection = connection
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.executed_sql = []
        self.executed_params = []
        self.mogrified_rows = []

    @property
    def encoding(self):
        return self._connection.encoding

    def cursor(self):
        return ProductionCallCountingCursor(
            self, self._connection.cursor()
        )

    def commit(self):
        self.commit_count += 1
        return self._connection.commit()

    def rollback(self):
        self.rollback_count += 1
        return self._connection.rollback()

    def reset_measured(self):
        self.execute_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.executed_sql.clear()
        self.executed_params.clear()
        self.mogrified_rows.clear()


class PollSideEffectRecorder:
    def __init__(self):
        self.lifecycle_publications = []
        self.desired_publications = []
        self.reconciler_notifications = 0
        self.final_outcomes = []
        self.phase_boundaries = []

    def record_lifecycle(self, event_type, broadcaster_id, login, rank):
        self.lifecycle_publications.append(
            (event_type, broadcaster_id, login, rank)
        )

    def record_desired(self, desired, broadcaster_ids):
        self.desired_publications.append(
            (dict(desired), dict(broadcaster_ids))
        )

    def record_notification(self):
        self.reconciler_notifications += 1

    def record_outcome(self, outcome):
        self.final_outcomes.append(outcome)

    def record_phase(self, phase, outcome):
        self.phase_boundaries.append((phase, outcome))


class RecordingDesiredStore:
    def __init__(
        self,
        delegate,
        recorder,
        *,
        read_error=None,
        publish_error=None,
        publish_error_after_apply=False,
    ):
        self.delegate = delegate
        self.recorder = recorder
        self.read_error = read_error
        self.publish_error = publish_error
        self.publish_error_after_apply = publish_error_after_apply

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.delegate.read()

    def read_generation(self):
        return self.delegate.read_generation()

    def publish(self, desired, broadcaster_ids):
        if self.publish_error is not None and not self.publish_error_after_apply:
            raise self.publish_error
        self.delegate.publish(desired, broadcaster_ids)
        self.recorder.record_desired(desired, broadcaster_ids)
        if self.publish_error is not None:
            raise self.publish_error


def make_poller(
    logins,
    fake_redis,
    reconciler=None,
    *,
    ranked_records=None,
    pool=None,
    twitch_error=None,
    desired_read_error=None,
    desired_publish_error=None,
    desired_publish_error_after_apply=False,
):
    """A StreamMonitoringService wired to recording in-memory test adapters.

    `logins` remains the compatibility shorthand. `ranked_records` supplies
    explicit ``(login, broadcaster_id)`` rows for duplicate and failure cases.
    """
    if ranked_records is None:
        ranked_records = [
            (login, 1000 + index) for index, login in enumerate(logins)
        ]
    service = StreamMonitoringService()
    service.twitch = FakeTwitch(
        [make_stream(login, broadcaster_id) for login, broadcaster_id in ranked_records],
        error=twitch_error,
    )
    service.redis_client = fake_redis
    recorder = PollSideEffectRecorder()
    service.desired_store = RecordingDesiredStore(
        RedisDesiredSetStore(fake_redis),
        recorder,
        read_error=desired_read_error,
        publish_error=desired_publish_error,
        publish_error_after_apply=desired_publish_error_after_apply,
    )
    service.reconciler = reconciler
    if reconciler is not None:
        notify = reconciler.notify_desired_changed

        def record_and_notify():
            recorder.record_notification()
            notify()

        reconciler.notify_desired_changed = MagicMock(
            side_effect=record_and_notify
        )
    service.db_pool = pool or CountingPool()
    service._get_clipping_disabled_ids = MagicMock(return_value=set())
    service._publish_lifecycle_event = MagicMock(
        side_effect=recorder.record_lifecycle
    )
    service._poll_observer = recorder
    service.test_side_effects = recorder
    return service


class TestDispatchInstrumentation:
    def test_cursor_proxy_observes_execute_values_internal_paging(self):
        connection = CountingConnection()
        rows = [(index, f"login{index}") for index in range(150)]
        execute_values(
            connection.cursor(),
            "INSERT INTO streamers (streamer_id, streamer_login, last_seen_at) "
            "VALUES %s",
            rows,
            template="(%s, %s, NOW())",
        )

        assert connection.execute_count == 2
        assert len(connection.executed_sql) == 2
        assert len(connection.mogrified_rows) == 150


class TestStreamerMetadataBatch:
    def test_empty_batch_omits_pool_sql_transaction_and_streak_change(self):
        service = StreamMonitoringService()
        service.db_pool = CountingPool()
        service._metadata_consecutive_failures = 3

        result = service._upsert_streamer_batch([])

        assert result is None
        assert service.db_pool.getconn_count == 0
        assert service.db_pool.connection.execute_count == 0
        assert service.db_pool.connection.commit_count == 0
        assert service.db_pool.connection.rollback_count == 0
        assert service._metadata_consecutive_failures == 3

    def test_duplicate_id_last_occurrence_uses_one_unpaged_statement(self):
        service = StreamMonitoringService()
        service.db_pool = CountingPool()
        records = [
            (streamer_id, f"login-{streamer_id}")
            for streamer_id in range(1, 151)
        ]
        records.extend([(7, "replacement"), (7, "final-login")])

        assert service._upsert_streamer_batch(records) is True

        connection = service.db_pool.connection
        assert connection.execute_count == 1
        assert len(connection.mogrified_rows) == 150
        assert b"(7, 'final-login', NOW())" in connection.executed_sql[0]
        assert b"(7, 'replacement', NOW())" not in connection.executed_sql[0]

    def test_success_uses_database_clock_one_commit_and_clean_pool_return(self):
        service = StreamMonitoringService()
        service.db_pool = CountingPool()

        assert service._upsert_streamer_batch([(101, "first"), (202, "second")]) is True

        connection = service.db_pool.connection
        sql = connection.executed_sql[0]
        assert connection.execute_count == 1
        assert connection.commit_count == 1
        assert connection.rollback_count == 0
        assert service.db_pool.putconn_count == 1
        assert service.db_pool.returned_connections == [(connection, False)]
        assert b"VALUES " in sql
        assert b"NOW()" in sql
        assert b"last_seen_at = EXCLUDED.last_seen_at" in sql
        assert b"first_seen_at" not in sql
        assert b"allows_clipping" not in sql
        assert b"eventsub_refused_at" not in sql
        assert b"clipping_disabled_at" not in sql

    def test_statement_failure_rolls_back_once_and_logs_bounded_batch_context(
        self, caplog
    ):
        service = StreamMonitoringService()
        connection = CountingConnection(execute_error=ValueError("poison row"))
        service.db_pool = CountingPool(connection)

        with caplog.at_level(logging.ERROR):
            result = service._upsert_streamer_batch(
                [(1, "first"), (1, "replacement"), (2, "second")]
            )

        assert result is False
        assert connection.execute_count == 1
        assert connection.commit_count == 0
        assert connection.rollback_count == 1
        assert service.db_pool.putconn_count == 1
        assert service.db_pool.discard_count == 0
        assert service._metadata_consecutive_failures == 1
        record = next(
            record
            for record in caplog.records
            if record.message == "Streamer metadata batch failed"
        )
        assert record.input_batch_size == 3
        assert record.unique_batch_size == 2
        assert record.metadata_failure_streak == 1
        assert record.error_type == "ValueError"

    def test_malformed_input_is_visible_without_a_per_row_fallback(self, caplog):
        service = StreamMonitoringService()
        service.db_pool = CountingPool()

        with caplog.at_level(logging.ERROR):
            result = service._upsert_streamer_batch(
                [(1, "valid"), ("malformed-record",)]
            )

        assert result is False
        assert service.db_pool.getconn_count == 0
        assert service._metadata_consecutive_failures == 1
        record = next(
            record
            for record in caplog.records
            if record.message == "Streamer metadata batch failed"
        )
        assert record.input_batch_size == 2
        assert record.unique_batch_size == 1
        assert record.error_type == "ValueError"

    def test_successful_non_empty_batch_resets_streak_but_empty_does_not(self):
        service = StreamMonitoringService()
        service.db_pool = CountingPool(
            CountingConnection(execute_error=ValueError("transient"))
        )

        assert service._upsert_streamer_batch([(1, "first")]) is False
        assert service._metadata_consecutive_failures == 1

        service.db_pool.connection.execute_error = None
        assert service._upsert_streamer_batch([]) is None
        assert service._metadata_consecutive_failures == 1
        assert service._upsert_streamer_batch([(1, "first")]) is True
        assert service._metadata_consecutive_failures == 0
        assert (
            stream_monitoring_service.stream_metadata_consecutive_failures
            ._value.get()
            == 0
        )

    @pytest.mark.parametrize("rollback_fails", [False, True])
    def test_commit_unknown_never_reports_success_and_discards_if_rollback_fails(
        self, rollback_fails
    ):
        service = StreamMonitoringService()
        connection = CountingConnection(
            commit_error=ConnectionError("commit acknowledgement lost"),
            rollback_error=(
                ConnectionError("rollback failed") if rollback_fails else None
            ),
        )
        service.db_pool = CountingPool(connection)

        result = service._upsert_streamer_batch([(1, "first")])

        assert result is False
        assert connection.execute_count == 1
        assert connection.commit_count == 1
        assert connection.rollback_count == 1
        assert service.db_pool.putconn_count == 1
        assert service.db_pool.discard_count == int(rollback_fails)
        assert service._metadata_consecutive_failures == 1


def broadcaster_id_for(logins, login):
    return 1000 + logins.index(login)


class TestTwitchCredentials:
    """Tests for TwitchCredentials -- real file I/O via tmp_path, zero mocks
    except `requests`, patched at the module seam for refresh()."""

    def test_load_returns_token_record(self, tmp_path):
        """Successfully load a valid token file."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "test_access_token",
            "refresh_token": "test_refresh_token",
            "scopes": ["chat:read", "clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }))

        record = TwitchCredentials(token_file).load()

        assert record == TokenRecord(
            access_token="test_access_token",
            refresh_token="test_refresh_token",
            scopes=["chat:read", "clips:edit"],
            created_at="2026-01-11T00:00:00Z",
            updated_at=None,
        )

    def test_load_seed_tool_record_loads_cleanly(self, tmp_path):
        """A record shaped like seed_twitch_tokens.py writes it (created_at,
        no updated_at) loads without error."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "seeded_access",
            "refresh_token": "seeded_refresh",
            "scopes": ["chat:read"],
            "created_at": "2026-01-11T00:00:00Z",
        }))

        record = TwitchCredentials(token_file).load()

        assert record.access_token == "seeded_access"
        assert record.created_at == "2026-01-11T00:00:00Z"
        assert record.updated_at is None

    def test_load_missing_file_raises_error_naming_seed_tool(self, tmp_path):
        """A missing token file should point the operator at the seed tool."""
        token_file = tmp_path / "nonexistent.json"

        with pytest.raises(FileNotFoundError, match="seed_twitch_tokens.py"):
            TwitchCredentials(token_file).load()

    def test_load_malformed_json_raises_clear_error(self, tmp_path):
        """Malformed JSON should raise JSONDecodeError, not something opaque."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text("not valid json {{{")

        with pytest.raises(json.JSONDecodeError):
            TwitchCredentials(token_file).load()

    def test_load_missing_access_token_raises(self, tmp_path):
        """Raise ValueError when access_token is missing."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "refresh_token": "test_refresh_token",
            "scopes": ["chat:read"],
        }))

        with pytest.raises(ValueError, match="access_token"):
            TwitchCredentials(token_file).load()

    def test_load_missing_refresh_token_raises(self, tmp_path):
        """Raise ValueError when refresh_token is missing."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "test_access_token",
            "scopes": ["chat:read"],
        }))

        with pytest.raises(ValueError, match="refresh_token"):
            TwitchCredentials(token_file).load()

    def test_persist_preserves_scopes_and_created_at_without_prior_load(self, tmp_path):
        """This is the bug that exists today: a caller that never called
        `load` first must not blank out scopes/created_at on persist."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["chat:read", "clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }))

        record = TwitchCredentials(token_file).persist("new_access", "new_refresh")

        assert record.scopes == ["chat:read", "clips:edit"]
        assert record.created_at == "2026-01-11T00:00:00Z"
        data = json.loads(token_file.read_text())
        assert data["scopes"] == ["chat:read", "clips:edit"]
        assert data["created_at"] == "2026-01-11T00:00:00Z"

    def test_persist_sets_updated_at(self, tmp_path):
        """persist should stamp updated_at on every write."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))

        record = TwitchCredentials(token_file).persist("new_access", "new_refresh")

        assert record.updated_at is not None
        data = json.loads(token_file.read_text())
        assert data["updated_at"] == record.updated_at

    def test_persist_interrupted_write_leaves_previous_file_intact(self, tmp_path, caplog):
        """A failure between the temp-file write and the atomic replace must
        not corrupt or lose the previous file, and -- since persist() is
        best effort (pyTwitchAPI calls it from inside a request) -- must be
        logged at ERROR rather than raised."""
        token_file = tmp_path / "tokens.json"
        original = {
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["chat:read"],
            "created_at": "2026-01-11T00:00:00Z",
        }
        token_file.write_text(json.dumps(original))

        with patch("token_manager.os.replace", side_effect=OSError("simulated crash")):
            with caplog.at_level(logging.ERROR):
                TwitchCredentials(token_file).persist("new_access", "new_refresh")

        assert json.loads(token_file.read_text()) == original
        assert list(tmp_path.glob(".tmp-tokens-*")) == []
        assert any("could not be written" in r.message for r in caplog.records)

    def test_persist_sets_group_and_permissions_before_replace(self, tmp_path):
        """Issue 1 (KNOWN_ISSUES.md): mkstemp() always creates the temp file
        at 0600 owned by whoever wrote it, which locks the other container
        out on the very next read. Every write must chown the temp file to
        TWITCH_TOKEN_GID and chmod it 0640 before the atomic replace, so a
        write from either container's uid stays readable by the other."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))
        calls = []
        real_replace = os.replace

        def fake_chown(path, uid, gid):
            calls.append(("chown", path, gid))

        def fake_chmod(path, mode):
            calls.append(("chmod", path, mode))

        def fake_replace(src, dst):
            calls.append(("replace", src, dst))
            real_replace(src, dst)

        with patch("token_manager.os.chown", side_effect=fake_chown), \
             patch("token_manager.os.chmod", side_effect=fake_chmod), \
             patch("token_manager.os.replace", side_effect=fake_replace):
            TwitchCredentials(token_file).persist("new_access", "new_refresh")

        kinds = [c[0] for c in calls]
        assert kinds == ["chown", "chmod", "replace"], (
            "chown and chmod must happen before the atomic replace, or the "
            "published file is briefly at mkstemp's default 0600"
        )
        assert calls[0][2] == token_manager.TWITCH_TOKEN_GID
        assert calls[1][2] == 0o640

    @patch("token_manager.requests.post")
    def test_refresh_sets_group_and_permissions_before_replace(self, mock_post, tmp_path):
        """Same guarantee as persist(), for the refresh() write path -- this
        is the one Flink's 401 handler calls directly."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
        }
        calls = []

        def fake_chown(path, uid, gid):
            calls.append(("chown", path, gid))

        def fake_chmod(path, mode):
            calls.append(("chmod", path, mode))

        with patch("token_manager.os.chown", side_effect=fake_chown), \
             patch("token_manager.os.chmod", side_effect=fake_chmod):
            TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert calls == [
            ("chown", calls[0][1], token_manager.TWITCH_TOKEN_GID),
            ("chmod", calls[1][1], 0o640),
        ]

    @patch("token_manager.requests.post")
    def test_refresh_stores_rotated_refresh_token(self, mock_post, tmp_path):
        """refresh should store the new refresh token when Twitch rotates it."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["clips:edit"],
            "created_at": "2026-01-11T00:00:00Z",
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "rotated_refresh",
            "expires_in": 3600,
        }

        record = TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert record.access_token == "new_access"
        assert record.refresh_token == "rotated_refresh"
        assert record.scopes == ["clips:edit"]
        assert record.created_at == "2026-01-11T00:00:00Z"

    @patch("token_manager.requests.post")
    def test_refresh_keeps_old_refresh_token_when_omitted(self, mock_post, tmp_path):
        """refresh should keep the old refresh token when Twitch's response
        omits a new one."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access",
            "expires_in": 3600,
        }

        record = TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert record.access_token == "new_access"
        assert record.refresh_token == "old_refresh"

    @patch("token_manager.requests.post")
    def test_refresh_returns_the_new_token_even_if_it_cannot_be_written(
        self, mock_post, tmp_path, caplog
    ):
        """If secrets/ is not writable, refresh() still hands back the token
        it just minted (logged at ERROR) instead of raising -- a read-only
        secrets/ must not turn a good refresh into a hard failure."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": ["clips:edit"],
        }))
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {
            "access_token": "new_access", "refresh_token": "rotated", "expires_in": 3600,
        }

        with patch("token_manager.os.replace", side_effect=OSError("read-only fs")):
            with caplog.at_level(logging.ERROR):
                record = TwitchCredentials(token_file).refresh("client_id", "client_secret")

        assert record.access_token == "new_access"
        assert record.refresh_token == "rotated"
        assert json.loads(token_file.read_text())["access_token"] == "old_access"
        assert list(tmp_path.glob(".tmp-tokens-*")) == []
        assert any("could not be written" in r.message for r in caplog.records)

    def test_concurrent_refreshes_serialize(self, tmp_path):
        """Two TwitchCredentials instances refreshing the same file at once
        must not interleave -- one waits for the other, and the file is
        never torn."""
        token_file = tmp_path / "tokens.json"
        token_file.write_text(json.dumps({
            "access_token": "old_access",
            "refresh_token": "old_refresh",
            "scopes": [],
        }))

        intervals = []
        intervals_lock = threading.Lock()

        def fake_post(*args, **kwargs):
            start = time.monotonic()
            time.sleep(0.05)
            end = time.monotonic()
            with intervals_lock:
                intervals.append((start, end))
            response = MagicMock()
            response.status_code = 200
            response.json.return_value = {
                "access_token": f"access-{start}",
                "refresh_token": f"refresh-{start}",
                "expires_in": 3600,
            }
            return response

        results = []

        def run():
            creds = TwitchCredentials(token_file)
            results.append(creds.refresh("client_id", "client_secret"))

        with patch("token_manager.requests.post", side_effect=fake_post):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(results) == 2
        (s1, e1), (s2, e2) = intervals
        assert e1 <= s2 or e2 <= s1, "refreshes overlapped -- lock did not serialize them"

        # File is valid JSON with the winning refresh's values -- never torn.
        data = json.loads(token_file.read_text())
        assert data["access_token"] in {r.access_token for r in results}


class TestMessagePayload:
    """Tests for chat message payload structure."""

    def test_message_payload_structure(self):
        """Message payload should match expected Kafka schema."""
        payload = {
            "broadcaster_id": 12345,
            "timestamp": 1704067200000,
            "message_id": "uuid-string",
            "text": "message content",
            "user_id": 67890,
            "user_name": "viewer_name",
            "metadata": {
                "emotes": {},
                "badges": {},
                "is_subscriber": False,
                "is_mod": False,
            },
        }

        # Validate required fields exist
        assert "broadcaster_id" in payload
        assert "timestamp" in payload
        assert "message_id" in payload
        assert "text" in payload
        assert "user_id" in payload
        assert "user_name" in payload
        assert "metadata" in payload

        # Validate types
        assert isinstance(payload["broadcaster_id"], int)
        assert isinstance(payload["timestamp"], int)
        assert isinstance(payload["text"], str)
        assert isinstance(payload["metadata"], dict)

    def test_lifecycle_event_structure(self):
        """Lifecycle event payload should match expected schema."""
        event = {
            "event_type": "online",
            "broadcaster_id": 12345,
            "broadcaster_login": "streamer_name",
            "rank": 1,
            "timestamp": 1704067200,
        }

        assert event["event_type"] in ["online", "offline"]
        assert isinstance(event["broadcaster_id"], int)
        assert isinstance(event["broadcaster_login"], str)
        assert isinstance(event["rank"], int)
        assert isinstance(event["timestamp"], int)


class TestKafkaDelivery:
    """Tests for Kafka message delivery."""

    def test_kafka_message_keyed_by_broadcaster_id(self):
        """Kafka messages should be keyed by broadcaster_id for partitioning."""
        broadcaster_id = 12345
        key = str(broadcaster_id).encode("utf-8")

        # Verify key format
        assert key == b"12345"
        assert isinstance(key, bytes)

    def test_kafka_message_value_is_json(self):
        """Kafka message value should be valid JSON."""
        message = {
            "broadcaster_id": 12345,
            "timestamp": 1704067200000,
            "text": "test message",
        }

        value = json.dumps(message).encode("utf-8")

        # Verify we can decode and parse
        decoded = json.loads(value.decode("utf-8"))
        assert decoded["broadcaster_id"] == 12345


class TestRedisKeyManagement:
    """Tests for Redis key patterns and TTL management."""

    def test_redis_key_format(self):
        """Redis keys should follow expected pattern."""
        broadcaster_login = "ninja"
        redis_key = f"streamer:online:{broadcaster_login}"

        assert redis_key == "streamer:online:ninja"
        assert redis_key.startswith("streamer:online:")

    def test_streamer_ttl_value(self):
        """Streamer TTL should be 3 minutes (180 seconds)."""
        REDIS_STREAMER_TTL = 180
        assert REDIS_STREAMER_TTL == 180

    def test_offline_detection_via_ttl_expiry(self):
        """Offline detection relies on Redis key expiration."""
        # When TTL expires, key is deleted
        # Service checks key existence to determine online status
        # This tests the logic concept

        def check_online(redis_client, broadcaster_login):
            redis_key = f"streamer:online:{broadcaster_login}"
            return redis_client.exists(redis_key)

        # Mock Redis client
        mock_redis = MagicMock()
        mock_redis.exists.return_value = False

        is_online = check_online(mock_redis, "expired_streamer")
        assert not is_online


class TestPrometheusMetrics:
    """Tests for Prometheus metrics configuration."""

    def test_metric_labels(self):
        """Metrics should use correct label names."""
        # Verify expected label patterns
        chat_message_labels = ["broadcaster_id"]
        kafka_labels = ["topic"]
        api_error_labels = ["error_type"]

        assert "broadcaster_id" in chat_message_labels
        assert "topic" in kafka_labels
        assert "error_type" in api_error_labels


class TestGracefulShutdown:
    """Tests for graceful shutdown handling."""

    def test_shutdown_sequence(self):
        """Shutdown should occur in correct order."""
        shutdown_order = []

        # Simulate shutdown steps
        def stop_scheduler():
            shutdown_order.append("scheduler")

        def close_twitch():
            shutdown_order.append("twitch")

        def flush_kafka():
            shutdown_order.append("kafka")

        def close_db():
            shutdown_order.append("db")

        def close_redis():
            shutdown_order.append("redis")

        # Execute shutdown
        stop_scheduler()
        close_twitch()
        flush_kafka()
        close_db()
        close_redis()

        # Verify order
        assert shutdown_order[0] == "scheduler"
        assert shutdown_order[-1] == "redis"
        assert len(shutdown_order) == 5


class TestChannelThresholdConfig:
    """Tests for the env-configurable monitored-set size.

    These call resolve_thresholds() directly with an env mapping rather than
    reloading the module: module-level Prometheus collectors cannot be
    registered twice in one process, so the module is not reloadable.
    """

    def test_thresholds_default_to_15_and_30(self):
        """Absent env vars, the shipped defaults are unchanged."""
        assert stream_monitoring_service.resolve_thresholds({}) == (15, 30)

    def test_thresholds_read_from_environment(self):
        """The monitored set can be ramped without editing code."""
        join, leave = stream_monitoring_service.resolve_thresholds(
            {"JOIN_THRESHOLD": "300", "LEAVE_THRESHOLD": "400"}
        )
        assert (join, leave) == (300, 400)

    def test_inverted_band_is_rejected(self):
        """LEAVE below JOIN leaves no hysteresis, so every joined channel would
        be instantly leave-eligible -- thrashing chat once per poll."""
        with pytest.raises(ValueError, match="must be >= JOIN_THRESHOLD"):
            stream_monitoring_service.resolve_thresholds(
                {"JOIN_THRESHOLD": "100", "LEAVE_THRESHOLD": "50"}
            )

    def test_equal_thresholds_are_allowed(self):
        """A zero-width band is degenerate but not incoherent -- it is the
        accepted no-hysteresis case used by Feature 007's firm ceiling."""
        assert stream_monitoring_service.resolve_thresholds(
            {"JOIN_THRESHOLD": "400", "LEAVE_THRESHOLD": "400"}
        ) == (400, 400)

    def test_thresholds_above_the_dual_coverage_ceiling_are_rejected(self):
        """Configuration cannot consume the 100-slot reconnect headroom."""
        with pytest.raises(ValueError, match="preserve EventSub reconnect headroom"):
            stream_monitoring_service.resolve_thresholds(
                {"JOIN_THRESHOLD": "400", "LEAVE_THRESHOLD": "401"}
            )

    def test_zero_join_threshold_is_rejected(self):
        """Monitoring nothing is a misconfiguration, not a valid state."""
        with pytest.raises(ValueError, match="JOIN_THRESHOLD must be >= 1"):
            stream_monitoring_service.resolve_thresholds(
                {"JOIN_THRESHOLD": "0", "LEAVE_THRESHOLD": "30"}
            )

    def test_module_defaults_match_shipped_values(self):
        """The imported module still ships 15/30 for anyone not setting env."""
        assert stream_monitoring_service.JOIN_THRESHOLD == 15
        assert stream_monitoring_service.LEAVE_THRESHOLD == 30


class TestFetchBudget:
    """Tests for the paginated fetch budget.

    Regression guard: fetch_count was previously min(..., 100), which silently
    capped the monitored set at 100 however high LEAVE_THRESHOLD was set.
    """

    def test_single_page_keeps_the_original_timeout(self):
        """At the shipped 15/30 the behaviour is unchanged: one page, 10s."""
        pages, timeout = stream_monitoring_service.fetch_budget(50)
        assert pages == 1
        assert timeout == stream_monitoring_service.GET_STREAMS_TIMEOUT_SECONDS

    def test_fetch_count_above_100_needs_multiple_pages(self):
        """Helix caps `first` at 100, so a larger set must paginate."""
        pages, _ = stream_monitoring_service.fetch_budget(520)
        assert pages == 6

    def test_timeout_scales_with_pages(self):
        """A larger threshold must not trip a bound sized for a single page."""
        _, one = stream_monitoring_service.fetch_budget(50)
        _, six = stream_monitoring_service.fetch_budget(520)
        assert six > one

    def test_timeout_never_reaches_the_poll_interval(self):
        """A stalled fetch must not eat several poll cycles at any threshold."""
        for fetch_count in (50, 520, 2020, 100_000):
            _, timeout = stream_monitoring_service.fetch_budget(fetch_count)
            assert timeout < stream_monitoring_service.POLL_INTERVAL_SECONDS

    def test_capped_timeout_still_far_exceeds_measured_cost(self):
        """The cap must stay a wide margin over the ~0.1s-per-page measurement,
        or a healthy large fetch would start timing out."""
        pages, timeout = stream_monitoring_service.fetch_budget(2020)
        assert pages == 21
        assert timeout >= pages * 0.1 * 10


def seed_desired(fake_redis, logins_with_ids):
    """Write a desired set the way the poller writes it, for reconciler tests."""
    desired = {
        login: rank for rank, (login, _) in enumerate(logins_with_ids, 1)
    }
    ids = {
        login: broadcaster_id for login, broadcaster_id in logins_with_ids
    }
    RedisDesiredSetStore(fake_redis).publish(desired, ids)


def make_reconciler(transport, fake_redis, refusal_store=None, **config_overrides):
    """A reconciler with test-speed timings: no real backoff, no idle wait."""
    settings = {
        "concurrency": 10,
        "idle_timeout_seconds": 0.01,
        "rate_limit_backoff_seconds": 0.0,
        "max_retry_rounds": 20,
    }
    settings.update(config_overrides)
    return Reconciler(
        transport=transport,
        desired_store=RedisDesiredSetStore(fake_redis),
        config=ReconcilerConfig(**settings),
        refusal_store=refusal_store,
    )


def counter_value(reason):
    """Read one labelled Counter sample, for before/after deltas."""
    return reconciler_module.subscription_create_failures_total.labels(
        reason=reason
    )._value.get()


def gauge_label_values(metric, label_name):
    """Read a labelled Gauge without creating labels as a side effect."""
    return {
        sample.labels[label_name]: sample.value
        for family in metric.collect()
        for sample in family.samples
        if sample.name == metric._name and label_name in sample.labels
    }


class TestDesiredSetHysteresis:
    """T011 / FR-011 -- the hysteresis band survives the move.

    The band used to live in the join loop, where `joined_channels` was the
    state. It now lives in the desired-set computation, where the previous
    desired set is the state. The behaviour must not change: entry at top
    JOIN_THRESHOLD, exit only below top LEAVE_THRESHOLD, and the 16-30 band
    retained in between.
    """

    @staticmethod
    def _legacy_joined_after_poll(ranked, joined, join_threshold, leave_threshold):
        """What the old IRC join loop settled on, as set algebra.

        Lifted straight from the old code path: join everything newly in the
        top JOIN_THRESHOLD, leave everything joined that fell out of the top
        LEAVE_THRESHOLD, keep the rest.
        """
        top_join = {login for rank, login in enumerate(ranked, 1) if rank <= join_threshold}
        top_leave = {login for rank, login in enumerate(ranked, 1) if rank <= leave_threshold}
        to_join = top_join - joined
        to_leave = joined - top_leave
        return (joined | to_join) - to_leave

    def test_login_enters_on_reaching_the_join_threshold(self):
        """Top JOIN_THRESHOLD is the entry condition."""
        ranked = [f"s{i}" for i in range(1, 31)]

        desired = compute_desired_set(ranked, previous_desired=set(), join_threshold=15,
                                      leave_threshold=30)

        assert "s1" in desired
        assert "s15" in desired
        assert desired["s15"] == 15

    def test_login_in_the_band_cannot_enter(self):
        """Rank 16-30 is a RETAINING band, not an entry one. A newcomer there
        must stay out, or the band would quietly become the join threshold."""
        ranked = [f"s{i}" for i in range(1, 31)]

        desired = compute_desired_set(ranked, previous_desired=set(), join_threshold=15,
                                      leave_threshold=30)

        assert "s16" not in desired
        assert "s30" not in desired
        assert len(desired) == 15

    def test_band_member_is_retained(self):
        """A login already wanted stays through ranks 16-30. This is the whole
        point of hysteresis: it protects the Flink baseline the channel built."""
        ranked = ["newtop"] + [f"s{i}" for i in range(1, 30)]
        # "s15" now sits at rank 16, inside the retained band.
        assert ranked[15] == "s15"

        desired = compute_desired_set(ranked, previous_desired={"s15"}, join_threshold=15,
                                      leave_threshold=30)

        assert "s15" in desired
        assert desired["s15"] == 16

    def test_login_leaves_only_after_exiting_the_leave_threshold(self):
        """Falling past rank 30 is the exit condition, and nothing sooner."""
        ranked = [f"s{i}" for i in range(1, 31)] + ["faller"]

        retained = compute_desired_set(ranked[:30] + ["x"], previous_desired={"s30"},
                                       join_threshold=15, leave_threshold=30)
        dropped = compute_desired_set(["newcomer"] + ranked[:30], previous_desired={"s30"},
                                      join_threshold=15, leave_threshold=30)

        assert "s30" in retained, "rank 30 is still inside the band"
        assert "s30" not in dropped, "rank 31 is outside the band"

    def test_login_absent_from_the_ranking_is_dropped(self):
        """A streamer that went offline leaves the set, band or no band."""
        desired = compute_desired_set(["a", "b"], previous_desired={"a", "b", "gone"},
                                      join_threshold=15, leave_threshold=30)

        assert "gone" not in desired

    def test_ranking_longer_than_the_leave_threshold_is_truncated(self):
        """Nothing past LEAVE_THRESHOLD can be wanted, however long the list."""
        ranked = [f"s{i}" for i in range(1, 51)]

        desired = compute_desired_set(ranked, previous_desired=set(ranked), join_threshold=15,
                                      leave_threshold=30)

        assert max(desired.values()) == 30
        assert len(desired) == 30

    def test_matches_the_old_join_loop_on_random_rankings(self):
        """Byte-equivalence to today, checked against the old set algebra over
        many random rank shuffles rather than a handful of chosen cases."""
        import random as _random

        rng = _random.Random(20260828)
        population = [f"s{i}" for i in range(60)]

        for _ in range(300):
            join_threshold = rng.randint(1, 20)
            leave_threshold = join_threshold + rng.randint(0, 20)
            ranked = rng.sample(population, leave_threshold)
            previous = set(rng.sample(population, rng.randint(0, 30)))

            new = set(compute_desired_set(ranked, previous, join_threshold, leave_threshold))
            legacy = self._legacy_joined_after_poll(ranked, previous, join_threshold,
                                                    leave_threshold)

            assert new == legacy, (
                f"diverged at join={join_threshold} leave={leave_threshold}: "
                f"{new ^ legacy}"
            )


class TestPollWritesIntentOnly:
    """T008 / T010 -- FR-002 and FR-003.

    The poll ranks, writes intent, and returns. It makes no chat connection and
    no subscription, and its cost does not follow the size of the change.
    """

    def test_poll_writes_the_desired_set_and_bumps_the_generation(self):
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)

        asyncio.run(service.poll_top_streams())

        assert service.desired_store.read() == DesiredSet(
            logins=logins[:15],
            ids={
                login: broadcaster_id_for(logins, login) for login in logins[:15]
            },
            generation=1,
        )

    def test_desired_set_reads_back_in_rank_order(self):
        """The reconciler works highest rank first."""
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)

        asyncio.run(service.poll_top_streams())

        assert service.desired_store.read().logins == logins[:15]

    def test_poll_makes_no_chat_connection_and_no_subscription(self):
        """FR-002. The poll path must not touch the transport at all."""
        fake_redis = FakeRedis()
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis, reconciler=reconciler)

        asyncio.run(service.poll_top_streams())

        assert transport.create_calls == []
        assert transport.delete_calls == []

    def test_poll_signals_the_reconciler(self):
        """The in-process fast path: the poller nudges the loop after writing."""
        fake_redis = FakeRedis()
        reconciler = make_reconciler(StubTransport(), fake_redis)
        service = make_poller(["a", "b"], fake_redis, reconciler=reconciler)

        asyncio.run(service.poll_top_streams())

        assert reconciler._wake.is_set()

    def test_poll_duration_does_not_scale_with_change_size(self):
        """FR-003. Both polls rank 500 streams. The first changes all 500, the
        second changes nothing. The work must be the same either way -- that is
        what "the poller writes intent" has to mean in practice.

        The operation count is the real assertion; wall clock is a loose
        backstop, because a per-channel network loop would blow past it."""
        logins = [f"s{i}" for i in range(1, 501)]
        fake_redis = FakeRedis()

        with patch.object(stream_monitoring_service, "JOIN_THRESHOLD", 500), \
             patch.object(stream_monitoring_service, "LEAVE_THRESHOLD", 500):
            service = make_poller(logins, fake_redis)

            fake_redis.calls.clear()
            start = time.perf_counter()
            asyncio.run(service.poll_top_streams())
            full_change_seconds = time.perf_counter() - start
            full_change_calls = list(fake_redis.calls)

            # Same ranking again: the desired set is already exactly right.
            service.twitch = FakeTwitch(
                [make_stream(login, 1000 + i) for i, login in enumerate(logins)]
            )
            fake_redis.calls.clear()
            start = time.perf_counter()
            asyncio.run(service.poll_top_streams())
            no_change_seconds = time.perf_counter() - start
            no_change_calls = list(fake_redis.calls)

        assert len(service.desired_store.read().logins) == 500
        assert len(full_change_calls) == len(no_change_calls), (
            "the poll did more work when more changed -- something in it "
            "still scales with the change, not the set"
        )
        assert full_change_calls.count("mget") == 1
        assert no_change_calls.count("mget") == 1
        assert full_change_calls.count("pipeline.execute") == 2
        assert no_change_calls.count("pipeline.execute") == 2
        assert full_change_seconds < no_change_seconds * 5 + 0.5, (
            f"500-change poll took {full_change_seconds:.4f}s against "
            f"{no_change_seconds:.4f}s for a 0-change poll"
        )

    def test_offline_lifecycle_events_carry_the_real_broadcaster_id(self):
        """A login only leaves the desired set by dropping out of this poll's
        ranking, so this poll never has an id for it. That made every offline
        event publish `broadcaster_id: 0` and key every one of them to
        partition b"0". Before Phase 3 an instance dict carried ids across
        polls; it went with the IRC client. The id map the last poll wrote to
        Redis has it."""
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)
        asyncio.run(service.poll_top_streams())
        expected_id = broadcaster_id_for(logins, "s15")

        # s15 leaves the ranking, and its online key has expired at
        # REDIS_STREAMER_TTL -- which is what makes the poll call it offline.
        fake_redis.delete("streamer:online:s15")
        remaining = [login for login in logins if login != "s15"]
        later = make_poller(remaining, fake_redis)
        asyncio.run(later.poll_top_streams())

        offline = [
            call for call in later._publish_lifecycle_event.call_args_list
            if call.args[0] == "offline"
        ]
        assert [call.args[2] for call in offline] == ["s15"]
        assert offline[0].args[1] == expected_id, (
            "the offline event published a placeholder id, so every offline "
            "event keys to the same Kafka partition"
        )

    def test_hysteresis_survives_across_polls_through_redis(self):
        """T011 end to end: the band is read back from Redis, so it survives a
        restart instead of collapsing to the join threshold."""
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        service = make_poller(logins, fake_redis)
        asyncio.run(service.poll_top_streams())
        assert service.desired_store.read().logins == logins[:15]

        # s15 slips to rank 16 -- inside the retained band, so it stays. A
        # brand-new service instance reads the band from Redis, not memory.
        reordered = ["newcomer"] + logins[:14] + ["s15"] + logins[15:]
        restarted = make_poller(reordered, fake_redis)
        asyncio.run(restarted.poll_top_streams())

        wanted = restarted.desired_store.read().logins
        assert "s15" in wanted, "the retained band did not survive the poll"
        assert "newcomer" in wanted
        assert "s16" not in wanted, "a newcomer entered through the band"

    def test_streamer_falling_out_of_the_band_is_dropped(self):
        fake_redis = FakeRedis()
        logins = [f"s{i}" for i in range(1, 31)]
        asyncio.run(make_poller(logins, fake_redis).poll_top_streams())

        # s1 disappears from the ranking altogether.
        asyncio.run(make_poller(logins[1:], fake_redis).poll_top_streams())

        assert "s1" not in RedisDesiredSetStore(fake_redis).read().logins


class TestBatchedPollOrchestration:
    @staticmethod
    def run_poll(service, *, join=2, leave=3):
        with patch.object(stream_monitoring_service, "JOIN_THRESHOLD", join), \
             patch.object(stream_monitoring_service, "LEAVE_THRESHOLD", leave), \
             patch.object(
                 stream_monitoring_service,
                 "CLIPPING_DISABLED_FETCH_PAD_FRACTION",
                 0.0,
             ):
            return asyncio.run(service.poll_top_streams())

    @staticmethod
    def clear_dispatches(fake_redis):
        fake_redis.calls.clear()
        fake_redis.dispatches.clear()
        fake_redis.mget_requests.clear()
        fake_redis.pipeline_executions.clear()

    def test_normalizes_once_and_preserves_partial_stable_turnover_semantics(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("retained", 10), ("departed", 20)])
        fake_redis.strings["streamer:online:retained"] = "10"
        fake_redis.strings["streamer:online:departed"] = "20"
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[
                ("NEW", 30),
                ("Retained", 10),
                ("outside", 40),
            ],
        )
        self.clear_dispatches(fake_redis)

        self.run_poll(service, join=1, leave=3)

        assert service.desired_store.read() == DesiredSet(
            logins=["new", "retained"],
            ids={"new": 30, "retained": 10},
            generation=2,
        )
        assert service.test_side_effects.lifecycle_publications == [
            ("online", 30, "new", 1)
        ]
        assert service.last_poll_result == {
            "outcome": "success",
            "ranked": 3,
            "desired": 2,
            "entered": 1,
            "left": 1,
            "join_threshold": 1,
            "leave_threshold": 3,
            "fetch_buffer": 0,
        }

    def test_duplicate_id_only_deduplicates_metadata_not_login_membership(self):
        fake_redis = FakeRedis()
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("first-login", 7), ("second-login", 7)],
        )

        self.run_poll(service, join=2, leave=2)

        assert service.desired_store.read() == DesiredSet(
            logins=["first-login", "second-login"],
            ids={"first-login": 7, "second-login": 7},
            generation=1,
        )
        assert service.db_pool.connection.execute_count == 1
        assert len(service.db_pool.connection.mogrified_rows) == 1
        assert (
            b"(7, 'second-login', NOW())"
            in service.db_pool.connection.executed_sql[0]
        )
        assert fake_redis.strings["streamer:online:first-login"] == "7"
        assert fake_redis.strings["streamer:online:second-login"] == "7"

    def test_membership_and_departures_precede_first_online_dispatch(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("departed", 99)])
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("current", 1)],
        )
        self.clear_dispatches(fake_redis)

        self.run_poll(service, join=1, leave=1)

        assert fake_redis.mget_requests == [[
            "streamer:online:current",
            "streamer:online:departed",
        ]]
        assert service.test_side_effects.phase_boundaries.index(
            ("metadata_persistence", "success")
        ) < service.test_side_effects.phase_boundaries.index(
            ("online_snapshot", "success")
        )

    def test_snapshot_is_one_ordered_unique_current_plus_departed_mget(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("gone", 90), ("duplicate", 80)])
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[
                ("Duplicate", 1),
                ("current", 2),
                ("duplicate", 3),
            ],
        )
        self.clear_dispatches(fake_redis)

        self.run_poll(service, join=3, leave=3)

        assert fake_redis.mget_requests == [[
            "streamer:online:duplicate",
            "streamer:online:current",
            "streamer:online:gone",
        ]]
        assert [
            dispatch
            for dispatch in fake_redis.dispatches
            if dispatch["phase"] == "online_snapshot"
        ] == [
            {
                "phase": "online_snapshot",
                "kind": "command",
                "operation": "mget",
            }
        ]

    @pytest.mark.parametrize("response", [[], [None, None, None]])
    def test_snapshot_length_mismatch_is_a_protocol_failure(self, response):
        fake_redis = FakeRedis()
        service = make_poller(["one", "two"], fake_redis)
        fake_redis.mget_response_override = response

        self.run_poll(service, join=2, leave=2)

        assert service.test_side_effects.final_outcomes == [
            "online_snapshot_failed"
        ]
        assert not [
            execution
            for execution in fake_redis.pipeline_executions
            if execution["phase"] == "online_refresh"
        ]
        assert service.test_side_effects.lifecycle_publications == []
        assert service.test_side_effects.desired_publications == []
        assert service.test_side_effects.reconciler_notifications == 0

    def test_refresh_uses_one_non_transactional_ranking_order_pipeline(self):
        fake_redis = FakeRedis()
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("first", 101), ("second", 202)],
        )

        self.run_poll(service, join=2, leave=2)

        refresh = next(
            execution
            for execution in fake_redis.pipeline_executions
            if execution["phase"] == "online_refresh"
        )
        assert refresh["transaction"] is False
        assert refresh["raise_on_error"] is True
        assert refresh["commands"] == [
            (
                "setex",
                ("streamer:online:first", 180, 101),
                {},
            ),
            (
                "setex",
                ("streamer:online:second", 180, 202),
                {},
            ),
        ]

    def test_repeated_login_uses_first_event_identity_and_last_refresh_value(self):
        fake_redis = FakeRedis()
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[
                ("Duplicate", 101),
                ("other", 202),
                ("duplicate", 303),
            ],
        )

        self.run_poll(service, join=3, leave=3)

        assert fake_redis.mget_requests == [[
            "streamer:online:duplicate",
            "streamer:online:other",
        ]]
        assert fake_redis.strings["streamer:online:duplicate"] == "303"
        assert [
            event
            for event in service.test_side_effects.lifecycle_publications
            if event[2] == "duplicate"
        ] == [("online", 101, "duplicate", 1)]

    def test_entry_outside_band_and_stable_channels_use_pre_refresh_snapshot(self):
        fake_redis = FakeRedis()
        fake_redis.strings["streamer:online:stable"] = "303"
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[
                ("inside", 101),
                ("outside", 202),
                ("stable", 303),
            ],
        )

        self.run_poll(service, join=1, leave=3)

        assert service.test_side_effects.lifecycle_publications == [
            ("online", 101, "inside", 1)
        ]
        assert fake_redis.strings["streamer:online:outside"] == "202"
        assert fake_redis.strings["streamer:online:stable"] == "303"

    def test_departed_present_is_silent_and_departed_absent_uses_previous_id(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("present", 101), ("expired", 202)])
        fake_redis.strings["streamer:online:present"] = "101"
        service = make_poller([], fake_redis)

        self.run_poll(service, join=1, leave=1)

        assert service.test_side_effects.lifecycle_publications == [
            ("offline", 202, "expired", 0)
        ]

    def test_invalid_departed_ids_suppress_only_corrupt_offline_events(
        self, caplog
    ):
        fake_redis = FakeRedis()
        fake_redis.zsets[DESIRED_KEY] = {
            "missing": 1.0,
            "nonnumeric": 2.0,
            "zero": 3.0,
            "negative": 4.0,
            "valid": 5.0,
        }
        fake_redis.hashes[DESIRED_IDS_KEY] = {
            "nonnumeric": "bad",
            "zero": "0",
            "negative": "-1",
            "valid": "505",
        }
        fake_redis.strings[DESIRED_GENERATION_KEY] = "7"
        service = make_poller([], fake_redis)

        with caplog.at_level(logging.ERROR):
            self.run_poll(service, join=1, leave=5)

        assert service.test_side_effects.lifecycle_publications == [
            ("offline", 505, "valid", 0)
        ]
        integrity_records = [
            record
            for record in caplog.records
            if record.message == "Offline lifecycle event suppressed: invalid previous id"
        ]
        assert {record.broadcaster_login for record in integrity_records} == {
            "missing",
            "nonnumeric",
            "zero",
            "negative",
        }
        assert all(
            event[1] > 0
            for event in service.test_side_effects.lifecycle_publications
        )

    @pytest.mark.parametrize(
        ("error", "expected_counter_label"),
        [
            (asyncio.TimeoutError(), "get_streams_timeout"),
            (RuntimeError("ranking failed"), "poll_streams"),
        ],
    )
    def test_ranking_failure_stops_all_downstream_work(
        self, error, expected_counter_label
    ):
        fake_redis = FakeRedis()
        service = make_poller([], fake_redis, twitch_error=error)
        counter = stream_monitoring_service.twitch_api_errors_total.labels(
            error_type=expected_counter_label
        )
        before = counter._value.get()

        self.run_poll(service)

        assert service.test_side_effects.final_outcomes == ["ranking_failed"]
        assert service.db_pool.getconn_count == 0
        assert fake_redis.dispatches == []
        assert service.test_side_effects.lifecycle_publications == []
        assert service.test_side_effects.desired_publications == []
        assert service.test_side_effects.reconciler_notifications == 0
        assert counter._value.get() - before == 1

    def test_desired_read_failure_stops_metadata_and_state_work(self):
        fake_redis = FakeRedis()
        service = make_poller(
            ["one"],
            fake_redis,
            desired_read_error=ConnectionError("desired read failed"),
        )

        self.run_poll(service, join=1, leave=1)

        assert service.test_side_effects.final_outcomes == [
            "desired_read_failed"
        ]
        assert service.db_pool.getconn_count == 0
        assert fake_redis.dispatches == []
        assert service.test_side_effects.desired_publications == []

    def test_snapshot_transport_failure_keeps_metadata_but_suppresses_downstream(self):
        fake_redis = FakeRedis()
        service = make_poller(["one"], fake_redis)
        fake_redis.fail_on = {"mget"}

        self.run_poll(service, join=1, leave=1)

        assert service.db_pool.connection.commit_count == 1
        assert service.test_side_effects.final_outcomes == [
            "online_snapshot_failed"
        ]
        assert service.test_side_effects.lifecycle_publications == []
        assert service.test_side_effects.desired_publications == []

    def test_refresh_failure_before_application_suppresses_all_downstream(self):
        fake_redis = FakeRedis()
        fake_redis.inject_pipeline_failure("online_refresh", when="before")
        service = make_poller(["one"], fake_redis)

        self.run_poll(service, join=1, leave=1)

        assert "streamer:online:one" not in fake_redis.strings
        assert service.test_side_effects.final_outcomes == [
            "online_refresh_failed"
        ]
        assert service.test_side_effects.lifecycle_publications == []
        assert service.test_side_effects.desired_publications == []
        assert service.test_side_effects.reconciler_notifications == 0

    def test_refresh_ack_loss_can_leave_keys_but_suppresses_downstream(self):
        fake_redis = FakeRedis()
        fake_redis.inject_pipeline_failure("online_refresh", when="after")
        service = make_poller(["one"], fake_redis)

        self.run_poll(service, join=1, leave=1)

        assert fake_redis.strings["streamer:online:one"] == "1000"
        assert service.test_side_effects.final_outcomes == [
            "online_refresh_failed"
        ]
        assert service.test_side_effects.lifecycle_publications == []
        assert service.test_side_effects.desired_publications == []
        assert service.test_side_effects.reconciler_notifications == 0

    def test_acknowledged_element_error_can_apply_other_refreshes_but_fails_phase(self):
        fake_redis = FakeRedis()
        fake_redis.inject_pipeline_response_error(
            "online_refresh", 1, ResponseError("bad second key")
        )
        service = make_poller(["one", "two", "three"], fake_redis)

        self.run_poll(service, join=3, leave=3)

        assert fake_redis.strings["streamer:online:one"] == "1000"
        assert "streamer:online:two" not in fake_redis.strings
        assert fake_redis.strings["streamer:online:three"] == "1002"
        assert service.test_side_effects.final_outcomes == [
            "online_refresh_failed"
        ]
        assert service.test_side_effects.lifecycle_publications == []
        assert service.test_side_effects.desired_publications == []

    @pytest.mark.parametrize("after_apply", [False, True])
    def test_desired_publish_failure_keeps_lifecycle_and_re_reads_visible_intent(
        self, after_apply
    ):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("old", 90)])
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("new", 101)],
            desired_publish_error=ConnectionError("desired publish failed"),
            desired_publish_error_after_apply=after_apply,
        )

        self.run_poll(service, join=1, leave=1)

        assert service.test_side_effects.lifecycle_publications == [
            ("online", 101, "new", 1),
            ("offline", 90, "old", 0),
        ]
        assert service.test_side_effects.final_outcomes == [
            "desired_publish_failed"
        ]
        assert service.test_side_effects.reconciler_notifications == 0
        visible = RedisDesiredSetStore(fake_redis).read()
        assert visible.logins == (["new"] if after_apply else ["old"])

        service.desired_store.publish_error = None
        self.run_poll(service, join=1, leave=1)

        assert service.test_side_effects.final_outcomes[-1] == "success"
        assert RedisDesiredSetStore(fake_redis).read().logins == ["new"]

    def test_recovery_after_failed_interval_uses_only_current_visible_state(self):
        fake_redis = FakeRedis()
        fake_redis.inject_pipeline_failure("online_refresh", when="after")
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("inside", 101), ("outside", 202)],
        )

        self.run_poll(service, join=1, leave=2)
        fake_redis.delete(
            "streamer:online:inside",
            "streamer:online:outside",
        )
        service.test_side_effects.lifecycle_publications.clear()
        self.run_poll(service, join=1, leave=2)

        assert service.test_side_effects.final_outcomes == [
            "online_refresh_failed",
            "success",
        ]
        assert service.test_side_effects.lifecycle_publications == [
            ("online", 101, "inside", 1)
        ]
        assert fake_redis.strings["streamer:online:outside"] == "202"

    def test_empty_empty_omits_metadata_snapshot_refresh_but_publishes_empty_intent(self):
        fake_redis = FakeRedis()
        service = make_poller([], fake_redis)
        self.clear_dispatches(fake_redis)

        self.run_poll(service, join=1, leave=1)

        assert service.db_pool.getconn_count == 0
        assert fake_redis.mget_requests == []
        assert not [
            execution
            for execution in fake_redis.pipeline_executions
            if execution["phase"] == "online_refresh"
        ]
        assert len(service.test_side_effects.desired_publications) == 1
        assert service.test_side_effects.desired_publications[0][0] == {}

    def test_departures_only_uses_one_snapshot_no_metadata_or_refresh(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("gone", 99)])
        service = make_poller([], fake_redis)
        self.clear_dispatches(fake_redis)

        self.run_poll(service, join=1, leave=1)

        assert service.db_pool.getconn_count == 0
        assert fake_redis.mget_requests == [["streamer:online:gone"]]
        assert not [
            execution
            for execution in fake_redis.pipeline_executions
            if execution["phase"] == "online_refresh"
        ]
        assert service.test_side_effects.lifecycle_publications == [
            ("offline", 99, "gone", 0)
        ]
        assert len(service.test_side_effects.desired_publications) == 1

    def test_metadata_failure_continues_healthy_state_intent_and_notification(self):
        fake_redis = FakeRedis()
        reconciler = MagicMock()
        connection = CountingConnection(
            execute_error=ValueError("poison metadata")
        )
        service = make_poller(
            ["one"],
            fake_redis,
            reconciler=reconciler,
            pool=CountingPool(connection),
        )

        self.run_poll(service, join=1, leave=1)

        assert connection.rollback_count == 1
        assert fake_redis.strings["streamer:online:one"] == "1000"
        assert service.test_side_effects.lifecycle_publications == [
            ("online", 1000, "one", 1)
        ]
        assert len(service.test_side_effects.desired_publications) == 1
        assert service.test_side_effects.reconciler_notifications == 1
        assert service.test_side_effects.final_outcomes == ["metadata_failed"]

    @pytest.mark.parametrize(
        ("fatal_phase", "expected_outcome"),
        [
            ("snapshot", "online_snapshot_failed"),
            ("desired_publish", "desired_publish_failed"),
        ],
    )
    def test_fatal_phase_outcome_takes_precedence_over_metadata_failure(
        self, fatal_phase, expected_outcome
    ):
        fake_redis = FakeRedis()
        if fatal_phase == "snapshot":
            fake_redis.fail_on = {"mget"}
        service = make_poller(
            ["one"],
            fake_redis,
            pool=CountingPool(
                CountingConnection(
                    execute_error=ValueError("metadata poison")
                )
            ),
            desired_publish_error=(
                ConnectionError("desired publish failed")
                if fatal_phase == "desired_publish"
                else None
            ),
        )

        self.run_poll(service, join=1, leave=1)

        assert service.test_side_effects.final_outcomes == [expected_outcome]
        assert service._metadata_consecutive_failures == 1
        assert service.test_side_effects.reconciler_notifications == 0


class TestFeature006DriverFoundation:
    @staticmethod
    def driver_args(driver, command, output, *extra):
        return driver.build_parser().parse_args([
            command,
            "--redis-url",
            "redis://isolated-host:6379/15",
            "--postgres-url",
            "postgresql://isolated-host/twitch_test",
            "--namespace",
            "feature006-test",
            "--confirm-isolated-targets",
            "--runtime-factory",
            "unused:test_factory",
            "--run-id",
            "offline-command-test",
            "--output",
            str(output),
            *extra,
        ])

    def test_fixture_modules_import_and_build_repeatable_disjoint_rankings(self):
        fixtures = importlib.import_module("phase5.feature006_fixtures")
        driver = importlib.import_module("phase5.feature006_driver")

        first_a = fixtures.build_ranking_fixture(
            500,
            "A",
            disabled_proportion=0.2,
            page_delay_ms=75.0,
        )
        second_a = fixtures.build_ranking_fixture(
            500,
            "A",
            disabled_proportion=0.2,
            page_delay_ms=75.0,
        )
        fixture_b = fixtures.build_ranking_fixture(
            500,
            "B",
            disabled_proportion=0.2,
            page_delay_ms=75.0,
        )

        assert first_a == second_a
        assert len(first_a.eligible_records) == 500
        assert first_a.page_size == 100
        assert all(len(page) <= 100 for page in first_a.pages)
        assert first_a.disabled_records > 0
        assert first_a.test_join_threshold == 500
        assert first_a.test_leave_threshold == 500
        assert first_a.test_fetch_buffer == first_a.disabled_records
        assert {
            record.streamer_id for record in first_a.records
        }.isdisjoint({
            record.streamer_id for record in fixture_b.records
        })
        assert {
            record.login for record in first_a.records
        }.isdisjoint({
            record.login for record in fixture_b.records
        })
        assert driver.fixture_summary(first_a)["eligible_records"] == 500

    def test_driver_supports_direct_help_and_package_import(self):
        driver = importlib.import_module("phase5.feature006_driver")
        script = (
            Path(__file__).parent
            / "phase5"
            / "feature006_driver.py"
        )

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        assert "operation-counts" in result.stdout
        assert {
            action.dest
            for action in driver.build_parser()._actions
        } >= {"help", "command"}

    def test_jsonl_envelope_percentile_and_bounded_record_kinds(self, tmp_path):
        driver = importlib.import_module("phase5.feature006_driver")
        output = tmp_path / "evidence.jsonl"
        writer = driver.JsonlWriter(output, run_id="test-run")

        row = writer.write(
            "operation-counts",
            {"case": "stable", "scale": 50},
        )

        assert driver.nearest_rank_p95(list(range(20, 0, -1))) == 19
        assert row["schema"] == "stream-scout.feature006.v1"
        assert row["run_id"] == "test-run"
        assert row["kind"] == "operation-counts"
        assert datetime.fromisoformat(row["timestamp"]).tzinfo is not None
        assert json.loads(output.read_text()) == row
        with pytest.raises(ValueError, match="record kind"):
            writer.write("login-name-as-kind", {})

    @pytest.mark.parametrize(
        ("redis_url", "postgres_url", "namespace"),
        [
            (None, "postgresql://host/twitch_test", "feature006-run"),
            ("redis://host:6379/0", "postgresql://host/twitch_test", "feature006-run"),
            ("redis://host:6379/15", "postgresql://host/twitch", "feature006-run"),
            ("redis://host:6379/15", "postgresql://host/twitch_test", "production"),
        ],
    )
    def test_isolated_target_preflight_rejects_missing_or_unsafe_targets(
        self, redis_url, postgres_url, namespace
    ):
        driver = importlib.import_module("phase5.feature006_driver")

        with pytest.raises(ValueError, match="isolated"):
            driver.validate_isolated_targets(
                redis_url=redis_url,
                postgres_url=postgres_url,
                namespace=namespace,
                isolation_acknowledged=True,
            )

    @pytest.mark.parametrize(
        ("redis_url", "postgres_url", "namespace"),
        [
            (
                "redis://production.example:6379/15",
                "postgresql://isolated.example/twitch_test",
                "feature006-test",
            ),
            (
                "redis://isolated.example:6379/15",
                "postgresql://production.example/contest",
                "feature006-test",
            ),
            (
                "redis://isolated.example:6379/15",
                "postgresql://isolated.example/twitch_test",
                "contest-prod",
            ),
        ],
    )
    def test_isolation_preflight_rejects_production_marker_bypasses(
        self, redis_url, postgres_url, namespace
    ):
        driver = importlib.import_module("phase5.feature006_driver")

        with pytest.raises(ValueError, match="isolated"):
            driver.validate_isolated_targets(
                redis_url=redis_url,
                postgres_url=postgres_url,
                namespace=namespace,
                isolation_acknowledged=True,
            )

    def test_isolation_preflight_requires_explicit_acknowledgement(self):
        driver = importlib.import_module("phase5.feature006_driver")

        with pytest.raises(ValueError, match="acknowledgement"):
            driver.validate_isolated_targets(
                redis_url="redis://isolated.example:6379/15",
                postgres_url="postgresql://isolated.example/twitch_test",
                namespace="feature006-test",
                isolation_acknowledged=False,
            )

    def test_calibration_requires_twenty_samples_and_builds_exact_budgets(self):
        fixtures = importlib.import_module("phase5.feature006_fixtures")
        driver = importlib.import_module("phase5.feature006_driver")
        fixture = fixtures.build_ranking_fixture(
            500,
            "A",
            disabled_proportion=0.2,
            page_delay_ms=20.0,
        )

        record = driver.build_calibration_record(
            scale=500,
            live_page_samples_ms=list(range(1, 21)),
            fixture=fixture,
            redis_rtt_samples_ms=[50.0] * 20,
            postgres_rtt_samples_ms=[75.0] * 20,
        )

        assert record["live_page_p95_ms"] == 19
        assert record["fixture_page_delay_ms"] == 20.0
        assert record["eligible_records"] == 500
        assert record["raw_records"] == len(fixture.records)
        assert record["disabled_records"] == fixture.disabled_records
        assert record["page_size"] == 100
        assert record["page_count"] == len(fixture.pages)
        assert record["ranking_budget_ms"] == len(fixture.pages) * 20.0
        assert record["non_ranking_budget_ms"] == (
            5000 - record["ranking_budget_ms"]
        )
        assert record["redis_median_ms"] == 50.0
        assert record["postgres_median_ms"] == 75.0
        assert record["acceptance_valid"] is True

        with pytest.raises(ValueError, match="20"):
            driver.build_calibration_record(
                scale=500,
                live_page_samples_ms=[1.0] * 19,
                fixture=fixture,
                redis_rtt_samples_ms=[50.0] * 20,
                postgres_rtt_samples_ms=[75.0] * 20,
            )

    def test_profile_measurements_replace_excluded_whole_polls(self):
        driver = importlib.import_module("phase5.feature006_driver")
        attempts = [
            {
                "duration_ms": 100.0,
                "excluded": False,
                "outcome": "success",
                "observed_eligible_records": 500,
                "effective_join_threshold": 500,
                "effective_leave_threshold": 500,
                "effective_fetch_buffer": 125,
                "phase_durations_ms": {"ranking_fetch": 10.0},
                "dispatch_counts": {"metadata_execute": 1},
                "overlap_skip_count": 0,
            },
            {
                "duration_ms": 999.0,
                "excluded": True,
                "phase_durations_ms": {},
                "dispatch_counts": {},
                "overlap_skip_count": 0,
            },
        ] + [
            {
                "duration_ms": float(index),
                "excluded": False,
                "outcome": "success",
                "observed_eligible_records": 500,
                "effective_join_threshold": 500,
                "effective_leave_threshold": 500,
                "effective_fetch_buffer": 125,
                "phase_durations_ms": {"ranking_fetch": float(index) / 2},
                "dispatch_counts": {"metadata_execute": 1},
                "overlap_skip_count": 0,
            }
            for index in range(1, 21)
        ]
        prepared = []

        record = driver.run_profile_measurements(
            scale=500,
            profile="stable",
            warmups=1,
            measured_polls=20,
            prepare_state=prepared.append,
            run_poll=lambda fixture_id: attempts.pop(0),
        )

        assert record["warmup_duration_ms"] == 100.0
        assert record["measured_durations_ms"] == [
            float(index) for index in range(1, 21)
        ]
        assert record["nearest_rank_p95_ms"] == 19.0
        assert record["excluded_poll_count"] == 1
        assert record["overlap_skip_count"] == 0
        assert set(prepared) == {"A"}
        assert attempts == []

    def test_turnover_profile_alternates_disjoint_fixture_ids(self):
        driver = importlib.import_module("phase5.feature006_driver")
        prepared = []

        record = driver.run_profile_measurements(
            scale=900,
            profile="complete_turnover",
            warmups=1,
            measured_polls=20,
            prepare_state=prepared.append,
            run_poll=lambda fixture_id: {
                "duration_ms": 10.0,
                "excluded": False,
                "outcome": "success",
                "observed_eligible_records": 900,
                "effective_join_threshold": 900,
                "effective_leave_threshold": 900,
                "effective_fetch_buffer": 225,
                "phase_durations_ms": {"ranking_fetch": 1.0},
                "dispatch_counts": {"metadata_execute": 1},
                "overlap_skip_count": 0,
            },
        )

        assert record["profile"] == "complete_turnover"
        assert prepared[1:5] == ["A", "B", "A", "B"]
        assert len(record["measured_durations_ms"]) == 20

    def test_profile_rejects_a_runtime_that_processed_the_wrong_scale(self):
        driver = importlib.import_module("phase5.feature006_driver")

        with pytest.raises(ValueError, match="observed eligible"):
            driver.run_profile_measurements(
                scale=500,
                profile="stable",
                warmups=1,
                measured_polls=20,
                prepare_state=lambda _fixture_id: None,
                run_poll=lambda _fixture_id: {
                    "duration_ms": 1.0,
                    "excluded": False,
                    "outcome": "success",
                    "observed_eligible_records": 300,
                    "effective_join_threshold": 500,
                    "effective_leave_threshold": 500,
                    "effective_fetch_buffer": 125,
                    "phase_durations_ms": {},
                    "dispatch_counts": {},
                    "overlap_skip_count": 0,
                },
            )

    def test_profile_rejects_a_caught_failed_poll_outcome(self):
        driver = importlib.import_module("phase5.feature006_driver")

        with pytest.raises(ValueError, match="successful poll outcome"):
            driver.run_profile_measurements(
                scale=500,
                profile="stable",
                warmups=1,
                measured_polls=20,
                prepare_state=lambda _fixture_id: None,
                run_poll=lambda _fixture_id: {
                    "duration_ms": 1.0,
                    "excluded": False,
                    "outcome": "desired_publish_failed",
                    "observed_eligible_records": 500,
                    "effective_join_threshold": 500,
                    "effective_leave_threshold": 500,
                    "effective_fetch_buffer": 125,
                    "phase_durations_ms": {},
                    "dispatch_counts": {},
                    "overlap_skip_count": 0,
                },
            )

    def test_pass_callback_gap_and_scheduler_event_recording(self):
        driver = importlib.import_module("phase5.feature006_driver")
        production_counts = []
        recorded_counts = []
        callback = driver.compose_pass_callbacks(
            production_counts.append,
            recorded_counts.append,
        )

        callback(500)

        assert production_counts == [500]
        assert recorded_counts == [500]
        assert driver.adjacent_gaps_ms(
            [1_000_000_000, 2_500_000_000, 5_000_000_000]
        ) == [1500.0, 2500.0]

        from apscheduler.events import (
            EVENT_JOB_ERROR,
            EVENT_JOB_EXECUTED,
            EVENT_JOB_MAX_INSTANCES,
            EVENT_JOB_MISSED,
        )

        recorder = driver.SchedulerEventRecorder(clock_ns=lambda: 123)
        for code in (
            EVENT_JOB_EXECUTED,
            EVENT_JOB_ERROR,
            EVENT_JOB_MISSED,
            EVENT_JOB_MAX_INSTANCES,
        ):
            recorder.record(type("Event", (), {"code": code})())

        assert [event["kind"] for event in recorder.events] == [
            "executed",
            "error",
            "missed",
            "max_instances",
        ]
        assert all(event["monotonic_ns"] == 123 for event in recorder.events)

    def test_recording_transport_delegates_and_records_rate_limit_and_progress(self):
        driver = importlib.import_module("phase5.feature006_driver")

        class Delegate:
            def __init__(self):
                self.create_calls = []
                self.delete_calls = []
                self.rate_limit_next = False

            async def list(self):
                for value in ("one", "two"):
                    yield value

            async def create(self, broadcaster_id):
                self.create_calls.append(broadcaster_id)
                if self.rate_limit_next:
                    self.rate_limit_next = False
                    raise RateLimitedError(retry_after=10.0)
                return f"subscription-{broadcaster_id}"

            async def delete(self, subscription_id):
                self.delete_calls.append(subscription_id)
                return True

        async def exercise():
            delegate = Delegate()
            ticks = iter(range(100, 1000, 100))
            proxy = driver.RecordingTransportProxy(
                delegate, clock_ns=lambda: next(ticks)
            )
            assert [item async for item in proxy.list()] == ["one", "two"]
            assert await proxy.create(1) == "subscription-1"
            delegate.rate_limit_next = True
            with pytest.raises(RateLimitedError):
                await proxy.create(2)
            assert await proxy.delete("subscription-1") is True
            proxy.record_backoff(
                seconds=10.0,
                coverage_before=1,
                coverage_after=2,
            )
            return delegate, proxy

        delegate, proxy = asyncio.run(exercise())

        assert delegate.create_calls == [1, 2]
        assert delegate.delete_calls == ["subscription-1"]
        assert len(proxy.accepted_creates) == 1
        assert len(proxy.rate_limit_events) == 1
        assert proxy.backoff_events[0]["coverage_after"] == 2

    def test_strict_evidence_validation_rejects_missing_contract_fields(self):
        driver = importlib.import_module("phase5.feature006_driver")
        valid_records = {
            "calibration": {
                "scale": 500,
                "live_page_samples_ms": [50.0] * 20,
                "live_page_p95_ms": 50.0,
                "fixture_page_delay_ms": 50.0,
                "raw_records": 625,
                "disabled_records": 125,
                "disabled_proportion": 0.2,
                "eligible_records": 500,
                "page_size": 100,
                "page_count": 7,
                "ranking_budget_ms": 350.0,
                "non_ranking_budget_ms": 4650.0,
                "redis_rtt_samples_ms": [50.0] * 20,
                "redis_median_ms": 50.0,
                "postgres_rtt_samples_ms": [75.0] * 20,
                "postgres_median_ms": 75.0,
            },
            "poll-profile": {
                "scale": 500,
                "profile": "stable",
                "warmup_outcome": "success",
                "poll_outcomes": ["success"] * 20,
                "observed_eligible_records": [500] * 20,
                "test_join_threshold": 500,
                "test_leave_threshold": 500,
                "test_fetch_buffer": 125,
                "warmup_duration_ms": 100.0,
                "measured_durations_ms": [100.0] * 20,
                "nearest_rank_p95_ms": 100.0,
                "overlap_skip_count": 0,
                "excluded_poll_count": 0,
                "phase_durations_ms": {
                    phase: [1.0] * 20
                    for phase in (
                        "ranking_fetch",
                        "metadata_persistence",
                        "online_snapshot",
                        "online_refresh",
                        "lifecycle_publication",
                        "desired_set_publication",
                    )
                },
                "dispatch_counts": {
                    "metadata_execute": [1] * 20,
                    "metadata_commit": [1] * 20,
                    "online_snapshot_mget": [1] * 20,
                    "online_refresh_execute": [1] * 20,
                },
            },
            "reconciler-gap": {
                "scale": 500,
                "post_convergence_started_at": "2026-08-31T00:00:00+00:00",
                "run_duration_seconds": 1800,
                "run_started_monotonic_ns": 0,
                "run_ended_monotonic_ns": 1_800_000_000_000,
                "converged_subscription_count": 500,
                "pass_completion_monotonic_ns": [
                    5_000_000_000,
                    1_795_000_000_000,
                ],
                "pass_completion_subscription_counts": [500, 500],
                "adjacent_gaps_ms": [1_790_000.0],
                "boundary_gaps_ms": [5_000.0, 5_000.0],
                "maximum_gap_ms": 1_790_000.0,
                "scheduler_events": [
                    {
                        "kind": "executed",
                        "monotonic_ns": index,
                        "job_id": "poll_streams",
                    }
                    for index in range(15)
                ],
            },
            "cold-start": {
                "target": 900,
                "initialization_complete_at": "2026-08-31T00:00:00+00:00",
                "initial_subscription_count": 0,
                "rate_limit_events": [{"at": 1}],
                "backoff_events": [{"coverage_before": 1, "coverage_after": 2}],
                "accepted_create_windows": [{"before": 1, "after": 2}],
                "subscription_count_by_window": [1, 2],
                "poll_start_end_monotonic_ns": [[1, 2]],
                "poll_durations_ms": [0.000001],
                "overlap_skip_count": 0,
                "scheduler_events": [
                    {
                        "kind": "executed",
                        "monotonic_ns": 2,
                        "job_id": "poll_streams",
                    }
                ],
                "effective_reconciler_config": {
                    "concurrency": 10,
                    "idle_timeout_seconds": 5.0,
                    "rate_limit_backoff_seconds": 10.0,
                    "max_retry_rounds": 20,
                    "readopt_interval_seconds": 300.0,
                    "adopt_retry_seconds": 30.0,
                },
                "final_subscription_count": 2,
            },
        }

        for kind, fields in valid_records.items():
            assert driver.validate_evidence_record(kind, fields) == fields
            incomplete = dict(fields)
            incomplete.pop(next(iter(fields)))
            with pytest.raises(ValueError, match="missing"):
                driver.validate_evidence_record(kind, incomplete)

        no_passes = dict(valid_records["reconciler-gap"])
        no_passes["pass_completion_monotonic_ns"] = []
        no_passes["adjacent_gaps_ms"] = []
        no_passes["boundary_gaps_ms"] = []
        no_passes["maximum_gap_ms"] = 0.0
        with pytest.raises(ValueError, match="pass completions"):
            driver.validate_evidence_record("reconciler-gap", no_passes)

        no_backoff = dict(valid_records["cold-start"])
        no_backoff["rate_limit_events"] = []
        with pytest.raises(ValueError, match="rate-limit"):
            driver.validate_evidence_record("cold-start", no_backoff)

        missed_poll = dict(valid_records["cold-start"])
        missed_poll["scheduler_events"] = [
            {
                "kind": "missed",
                "monotonic_ns": 2,
                "job_id": "poll_streams",
            }
        ]
        with pytest.raises(ValueError, match="scheduler failure"):
            driver.validate_evidence_record("cold-start", missed_poll)

        modified_policy = dict(valid_records["cold-start"])
        modified_policy["effective_reconciler_config"] = {
            **modified_policy["effective_reconciler_config"],
            "rate_limit_backoff_seconds": 0.0,
        }
        with pytest.raises(ValueError, match="production policy"):
            driver.validate_evidence_record("cold-start", modified_policy)

        wrong_scale = dict(valid_records["reconciler-gap"])
        wrong_scale["converged_subscription_count"] = 300
        with pytest.raises(ValueError, match="subscription count"):
            driver.validate_evidence_record("reconciler-gap", wrong_scale)

    def test_steady_state_boundary_gap_detects_a_reconciler_that_stops_early(self):
        driver = importlib.import_module("phase5.feature006_driver")
        scheduler_events = [
            {
                "kind": "executed",
                "monotonic_ns": index,
                "job_id": "poll_streams",
            }
            for index in range(15)
        ]

        record = driver.build_reconciler_gap_record(
            scale=500,
            post_convergence_started_at="2026-08-31T00:00:00Z",
            run_duration_seconds=1800,
            run_started_monotonic_ns=0,
            run_ended_monotonic_ns=1_800_000_000_000,
            converged_subscription_count=500,
            pass_completion_monotonic_ns=[
                5_000_000_000,
                10_000_000_000,
            ],
            pass_completion_subscription_counts=[500, 500],
            scheduler_events=scheduler_events,
        )

        assert record["adjacent_gaps_ms"] == [5_000.0]
        assert record["boundary_gaps_ms"] == [
            5_000.0,
            1_790_000.0,
        ]
        assert record["maximum_gap_ms"] == 1_790_000.0

    def test_operation_count_records_require_positive_exact_dispatches(self):
        driver = importlib.import_module("phase5.feature006_driver")
        successful = {
            "clipping_execute": 1,
            "redis_zrange": 1,
            "redis_hgetall": 1,
            "redis_get": 1,
            "metadata_execute": 1,
            "metadata_commit": 1,
            "online_snapshot_mget": 1,
            "online_refresh_execute": 1,
            "desired_publication_execute": 1,
        }

        record = driver.build_operation_count_record(
            case="stable",
            scale=500,
            counts=successful,
            outcome="success",
            observed_eligible_records=500,
            effective_join_threshold=500,
            effective_leave_threshold=500,
            effective_fetch_buffer=0,
        )

        assert record["acceptance_valid"] is True
        assert record["dispatch_counts"] == successful
        for boundary in (
            "metadata_execute",
            "metadata_commit",
            "online_snapshot_mget",
            "online_refresh_execute",
        ):
            invalid = dict(successful)
            invalid[boundary] = 0
            with pytest.raises(ValueError, match=boundary):
                driver.build_operation_count_record(
                    case="stable",
                    scale=500,
                    counts=invalid,
                    outcome="success",
                    observed_eligible_records=500,
                    effective_join_threshold=500,
                    effective_leave_threshold=500,
                    effective_fetch_buffer=0,
                )
        unexpected = dict(successful)
        unexpected["redis_exists"] = 500
        with pytest.raises(ValueError, match="unexpected"):
            driver.build_operation_count_record(
                case="stable",
                scale=500,
                counts=unexpected,
                outcome="success",
                observed_eligible_records=500,
                effective_join_threshold=500,
                effective_leave_threshold=500,
                effective_fetch_buffer=0,
            )
        with pytest.raises(ValueError, match="observed"):
            driver.build_operation_count_record(
                case="stable",
                scale=500,
                counts=successful,
                outcome="success",
                observed_eligible_records=300,
                effective_join_threshold=500,
                effective_leave_threshold=500,
                effective_fetch_buffer=0,
            )
        with pytest.raises(ValueError, match="successful poll outcome"):
            driver.build_operation_count_record(
                case="stable",
                scale=500,
                counts=successful,
                outcome="desired_publish_failed",
                observed_eligible_records=500,
                effective_join_threshold=500,
                effective_leave_threshold=500,
                effective_fetch_buffer=0,
            )

    def test_driver_proxies_count_actual_dispatch_boundaries(self):
        driver = importlib.import_module("phase5.feature006_driver")
        counter = driver.OperationCounter()
        redis_proxy = driver.RedisDispatchProxy(FakeRedis(), counter)

        redis_proxy.zrange("chat:desired", 0, -1)
        redis_proxy.hgetall("chat:desired:ids")
        redis_proxy.get("chat:desired:generation")
        redis_proxy.mget(["streamer:online:first"])
        refresh = redis_proxy.pipeline(transaction=False)
        refresh.setex("streamer:online:first", 180, 1)
        refresh.setex("streamer:online:second", 180, 2)
        refresh.execute()
        desired = redis_proxy.pipeline()
        desired.delete("chat:desired")
        desired.incr("chat:desired:generation")
        desired.execute()

        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor_proxy = driver.CursorDispatchProxy(cursor, counter)
        cursor_proxy.execute(
            "SELECT streamer_id FROM streamers "
            "WHERE allows_clipping = FALSE"
        )
        cursor_proxy.execute(
            "INSERT INTO streamers (streamer_id) VALUES (1) "
            "ON CONFLICT (streamer_id) DO UPDATE SET streamer_id = EXCLUDED.streamer_id"
        )
        connection = MagicMock()
        connection_proxy = driver.ConnectionDispatchProxy(
            connection, counter
        )
        connection_proxy.commit()

        assert counter.report() == dict(
            driver.NON_EMPTY_DISPATCH_COUNTS
        )

    @pytest.mark.parametrize(
        ("case", "snapshot_count"),
        [("empty-empty", 0), ("departures-only", 1)],
    )
    def test_empty_operation_count_records_enforce_required_omissions(
        self, case, snapshot_count
    ):
        driver = importlib.import_module("phase5.feature006_driver")

        record = driver.build_operation_count_record(
            case=case,
            scale=None,
            counts={
                "redis_zrange": 1,
                "redis_hgetall": 1,
                "redis_get": 1,
                "online_snapshot_mget": snapshot_count,
                "desired_publication_execute": 1,
            },
            outcome="success",
            observed_eligible_records=0,
            effective_join_threshold=1,
            effective_leave_threshold=1,
            effective_fetch_buffer=0,
        )

        assert record["dispatch_counts"]["metadata_execute"] == 0
        assert record["dispatch_counts"]["online_snapshot_mget"] == snapshot_count
        assert record["dispatch_counts"]["online_refresh_execute"] == 0
        assert record["dispatch_counts"]["desired_publication_execute"] == 1
        assert record["acceptance_valid"] is True

    def test_operation_counts_command_orchestrates_every_scale_and_empty_case(
        self, tmp_path
    ):
        driver = importlib.import_module("phase5.feature006_driver")
        output = tmp_path / "operation-counts.jsonl"
        args = self.driver_args(
            driver,
            "operation-counts",
            output,
            "--scales",
            "50",
            "500",
            "900",
            "--case",
            "stable",
            "--case",
            "complete-turnover",
            "--case",
            "empty-empty",
            "--case",
            "departures-only",
        )

        class Runtime:
            async def run_operation_count(
                self, *, case, scale, fixture, counter
            ):
                if case in {"stable", "complete_turnover"}:
                    assert fixture.scale == scale
                    expected = dict(driver.NON_EMPTY_DISPATCH_COUNTS)
                    observed = scale
                    join = leave = scale
                    buffer = 0
                else:
                    expected = dict(driver.EMPTY_DISPATCH_COUNTS)
                    if case == "departures_only":
                        expected["online_snapshot_mget"] = 1
                    observed = 0
                    join = leave = 1
                    buffer = 0
                for boundary, count in expected.items():
                    for _ in range(count):
                        counter.increment(boundary)
                return {
                    "outcome": "success",
                    "observed_eligible_records": observed,
                    "effective_join_threshold": join,
                    "effective_leave_threshold": leave,
                    "effective_fetch_buffer": buffer,
                }

        asyncio.run(
            driver._run_command(
                args,
                Runtime(),
                driver.JsonlWriter(output, run_id=args.run_id),
            )
        )

        records = [
            json.loads(line) for line in output.read_text().splitlines()
        ]
        assert len(records) == 8
        assert {record["kind"] for record in records} == {
            "operation-counts"
        }
        assert all(record["acceptance_valid"] for record in records)

    def test_operation_count_command_rejects_runtime_counter_disagreement(
        self, tmp_path
    ):
        driver = importlib.import_module("phase5.feature006_driver")
        output = tmp_path / "operation-counts.jsonl"
        args = self.driver_args(
            driver,
            "operation-counts",
            output,
            "--scales",
            "50",
            "--case",
            "stable",
        )

        class Runtime:
            def run_operation_count(
                self, *, case, scale, fixture, counter
            ):
                for boundary, count in (
                    driver.NON_EMPTY_DISPATCH_COUNTS.items()
                ):
                    for _ in range(count):
                        counter.increment(boundary)
                return {
                    "dispatch_counts": {},
                    "outcome": "success",
                    "observed_eligible_records": scale,
                    "effective_join_threshold": scale,
                    "effective_leave_threshold": scale,
                    "effective_fetch_buffer": 0,
                }

        with pytest.raises(ValueError, match="disagree"):
            asyncio.run(
                driver._run_command(
                    args,
                    Runtime(),
                    driver.JsonlWriter(output, run_id=args.run_id),
                )
            )

    def test_calibrate_command_collects_live_and_datastore_samples(self, tmp_path):
        driver = importlib.import_module("phase5.feature006_driver")
        output = tmp_path / "calibration.jsonl"
        args = self.driver_args(
            driver,
            "calibrate",
            output,
            "--minimum-page-samples",
            "20",
        )

        class Runtime:
            def __init__(self):
                self.pages = 0
                self.redis_calls = 0
                self.postgres_calls = 0

            async def fetch_live_page(self, *, first, cursor):
                assert first == 100
                self.pages += 1
                return {"cursor": self.pages}

            def redis_round_trip(self):
                self.redis_calls += 1

            def postgres_round_trip(self):
                self.postgres_calls += 1

            def observed_disabled_proportion(self):
                return 0.2

        runtime = Runtime()
        asyncio.run(
            driver._run_command(
                args,
                runtime,
                driver.JsonlWriter(output, run_id=args.run_id),
            )
        )

        records = [
            json.loads(line) for line in output.read_text().splitlines()
        ]
        assert runtime.pages == 20
        assert runtime.redis_calls == 20
        assert runtime.postgres_calls == 20
        assert [record["scale"] for record in records] == [500, 900]
        assert all(record["eligible_records"] == record["scale"] for record in records)

    def test_poll_profile_command_runs_warmup_and_twenty_complete_polls(
        self, tmp_path
    ):
        fixtures = importlib.import_module("phase5.feature006_fixtures")
        driver = importlib.import_module("phase5.feature006_driver")
        calibration_path = tmp_path / "calibration.jsonl"
        fixture = fixtures.build_ranking_fixture(
            500,
            "A",
            disabled_proportion=0.2,
            page_delay_ms=50.0,
        )
        calibration = driver.build_calibration_record(
            500,
            [50.0] * 20,
            fixture,
            [50.0] * 20,
            [75.0] * 20,
        )
        driver.JsonlWriter(
            calibration_path, run_id="calibration"
        ).write("calibration", calibration)
        output = tmp_path / "profile.jsonl"
        args = self.driver_args(
            driver,
            "poll-profile",
            output,
            "--scale",
            "500",
            "--profile",
            "stable",
            "--warmups",
            "1",
            "--measured-polls",
            "20",
            "--calibration",
            str(calibration_path),
        )

        class Runtime:
            def __init__(self):
                self.prepared = []
                self.polls = 0

            def prepare_profile_state(
                self, *, profile, fixture, opposite_fixture
            ):
                assert profile == "stable"
                assert fixture.fixture_id == "A"
                assert opposite_fixture.fixture_id == "B"
                self.prepared.append(fixture.fixture_id)

            def run_profile_poll(self, fixture):
                self.polls += 1
                return {
                    "excluded": False,
                    "outcome": "success",
                    "observed_eligible_records": 500,
                    "effective_join_threshold": 500,
                    "effective_leave_threshold": 500,
                    "effective_fetch_buffer": fixture.test_fetch_buffer,
                    "overlap_skip_count": 0,
                    "phase_durations_ms": {
                        phase: 1.0 for phase in driver.PROFILE_PHASES
                    },
                    "dispatch_counts": {
                        "metadata_execute": 1,
                        "metadata_commit": 1,
                        "online_snapshot_mget": 1,
                        "online_refresh_execute": 1,
                    },
                }

        runtime = Runtime()
        asyncio.run(
            driver._run_command(
                args,
                runtime,
                driver.JsonlWriter(output, run_id=args.run_id),
            )
        )

        record = json.loads(output.read_text())
        assert runtime.polls == 21
        assert runtime.prepared == ["A"] * 21
        assert len(record["measured_durations_ms"]) == 20
        assert record["acceptance_valid"] is True

    def test_steady_state_command_composes_callbacks_and_uses_direct_gaps(
        self, tmp_path
    ):
        driver = importlib.import_module("phase5.feature006_driver")
        output = tmp_path / "steady.jsonl"
        args = self.driver_args(
            driver,
            "steady-state",
            output,
            "--scale",
            "500",
            "--minutes",
            "30",
        )

        class Runtime:
            def __init__(self):
                self.production_counts = []
                self.completion_times = [
                    seconds * 1_000_000_000
                    for seconds in range(10, 1800, 10)
                ]
                self._clocks = iter(
                    [0, *self.completion_times, 1_800_000_000_000]
                )
                self.production_pass_callback = self.production_counts.append

            def monotonic_ns(self):
                return next(self._clocks)

            def wait_for_convergence(self, scale):
                assert scale == 500
                return {
                    "started_at": "2026-08-31T00:00:00Z",
                    "subscription_count": 500,
                }

            def run_steady_state(
                self,
                *,
                scale,
                duration_seconds,
                pass_callback,
                scheduler_callback,
            ):
                assert scale == 500
                assert duration_seconds == 1800
                for _ in self.completion_times:
                    pass_callback(500)
                from apscheduler.events import EVENT_JOB_EXECUTED

                for _ in range(15):
                    scheduler_callback(
                        type(
                            "Event",
                            (),
                            {
                                "code": EVENT_JOB_EXECUTED,
                                "job_id": "poll_streams",
                            },
                        )()
                    )

        runtime = Runtime()
        asyncio.run(
            driver._run_command(
                args,
                runtime,
                driver.JsonlWriter(output, run_id=args.run_id),
            )
        )

        record = json.loads(output.read_text())
        assert runtime.production_counts == [500] * len(
            runtime.completion_times
        )
        assert record["run_started_monotonic_ns"] == 0
        assert record["run_ended_monotonic_ns"] == 1_800_000_000_000
        assert len(record["pass_completion_monotonic_ns"]) == len(
            runtime.completion_times
        )
        assert record["run_duration_seconds"] == 1800
        assert record["maximum_gap_ms"] == 10_000.0
        assert record["acceptance_valid"] is True

    def test_cold_start_command_enforces_initialized_zero_state_and_progress(
        self, tmp_path
    ):
        driver = importlib.import_module("phase5.feature006_driver")
        output = tmp_path / "cold.jsonl"
        args = self.driver_args(
            driver,
            "cold-start",
            output,
            "--scale",
            "900",
            "--require-rate-limit-backoff",
        )

        class Delegate:
            async def list(self):
                if False:
                    yield None

            async def create(self, broadcaster_id):
                return broadcaster_id

            async def delete(self, subscription_id):
                return subscription_id

        class Runtime:
            def initialize_cold_start(self, *, target):
                assert target == 900
                return {
                    "process_initialized": True,
                    "pools_warm": True,
                    "transport_started": True,
                    "subscription_count": 0,
                    "desired_count": 900,
                    "initialization_complete_at": "2026-08-31T00:00:00Z",
                    "transport": Delegate(),
                    "reconciler_config": dict(
                        driver.PRODUCTION_RECONCILER_CONFIG
                    ),
                }

            async def run_cold_start(
                self,
                *,
                target,
                transport,
                poll_recorder,
                scheduler_callback,
            ):
                assert target == 900
                transport.rate_limit_events.append({"monotonic_ns": 1})
                transport.record_backoff(
                    seconds=10.0,
                    coverage_before=0,
                    coverage_after=1,
                )
                await poll_recorder.run(lambda: None)
                from apscheduler.events import EVENT_JOB_EXECUTED

                scheduler_callback(
                    type(
                        "Event",
                        (),
                        {
                            "code": EVENT_JOB_EXECUTED,
                            "job_id": "poll_streams",
                        },
                    )()
                )
                return 1

        asyncio.run(
            driver._run_command(
                args,
                Runtime(),
                driver.JsonlWriter(output, run_id=args.run_id),
            )
        )

        record = json.loads(output.read_text())
        assert record["initial_subscription_count"] == 0
        assert record["overlap_skip_count"] == 0
        assert record["final_subscription_count"] == 1
        assert record["effective_reconciler_config"] == dict(
            driver.PRODUCTION_RECONCILER_CONFIG
        )
        assert record["acceptance_valid"] is True

    def test_driver_cold_start_policy_matches_production_defaults(self):
        driver = importlib.import_module("phase5.feature006_driver")
        config = reconciler_module.resolve_reconciler_config({})

        assert driver.PRODUCTION_RECONCILER_CONFIG == {
            "concurrency": config.concurrency,
            "idle_timeout_seconds": config.idle_timeout_seconds,
            "rate_limit_backoff_seconds": config.rate_limit_backoff_seconds,
            "max_retry_rounds": config.max_retry_rounds,
            "readopt_interval_seconds": config.readopt_interval_seconds,
            "adopt_retry_seconds": config.adopt_retry_seconds,
        }


class TestPollDispatchCounts:
    @staticmethod
    def run_at_scale(service, scale):
        with patch.object(stream_monitoring_service, "JOIN_THRESHOLD", scale), \
             patch.object(stream_monitoring_service, "LEAVE_THRESHOLD", scale), \
             patch.object(
                 stream_monitoring_service,
                 "CLIPPING_DISABLED_FETCH_PAD_FRACTION",
                 0.0,
             ):
            asyncio.run(service.poll_top_streams())

    @staticmethod
    def assert_non_empty_fixed_counts(service, fake_redis):
        assert service.db_pool.connection.execute_count == 1
        assert service.db_pool.connection.commit_count == 1
        assert sum(
            dispatch["phase"] == "online_snapshot"
            for dispatch in fake_redis.dispatches
        ) == 1
        assert sum(
            execution["phase"] == "online_refresh"
            for execution in fake_redis.pipeline_executions
        ) == 1
        assert sum(
            execution["phase"] == "desired_set_publication"
            for execution in fake_redis.pipeline_executions
        ) == 1

    @pytest.mark.parametrize("scale", [50, 500, 900])
    def test_stable_profiles_have_positive_constant_dispatch_counts(self, scale):
        logins = [f"stable-{index}" for index in range(scale)]
        fake_redis = FakeRedis()
        seed_desired(
            fake_redis,
            [
                (login, 1000 + index)
                for index, login in enumerate(logins)
            ],
        )
        fake_redis.strings.update(
            {
                f"streamer:online:{login}": str(1000 + index)
                for index, login in enumerate(logins)
            }
        )
        service = make_poller(logins, fake_redis)
        fake_redis.calls.clear()
        fake_redis.dispatches.clear()
        fake_redis.mget_requests.clear()
        fake_redis.pipeline_executions.clear()
        service.db_pool.reset_measured()

        self.run_at_scale(service, scale)

        self.assert_non_empty_fixed_counts(service, fake_redis)
        assert len(fake_redis.mget_requests[0]) == scale

    @pytest.mark.parametrize("scale", [50, 500, 900])
    def test_complete_turnover_exposes_no_hidden_sql_or_redis_paging(self, scale):
        current = [f"current-{index}" for index in range(scale)]
        previous = [
            (f"departed-{index}", 100_000 + index)
            for index in range(scale)
        ]
        fake_redis = FakeRedis()
        seed_desired(fake_redis, previous)
        service = make_poller(current, fake_redis)
        fake_redis.calls.clear()
        fake_redis.dispatches.clear()
        fake_redis.mget_requests.clear()
        fake_redis.pipeline_executions.clear()
        service.db_pool.reset_measured()

        self.run_at_scale(service, scale)

        self.assert_non_empty_fixed_counts(service, fake_redis)
        assert len(fake_redis.mget_requests[0]) == scale * 2
        assert len(service.db_pool.connection.mogrified_rows) == scale

    @pytest.mark.parametrize(
        ("previous", "expected"),
        [
            ([], (0, 0, 0, 1)),
            ([("departed", 99)], (0, 1, 0, 1)),
        ],
    )
    def test_empty_dispatch_omissions_still_publish_desired_intent(
        self, previous, expected
    ):
        fake_redis = FakeRedis()
        if previous:
            seed_desired(fake_redis, previous)
        service = make_poller([], fake_redis)
        fake_redis.calls.clear()
        fake_redis.dispatches.clear()
        fake_redis.mget_requests.clear()
        fake_redis.pipeline_executions.clear()
        service.db_pool.reset_measured()

        self.run_at_scale(service, 1)

        actual = (
            service.db_pool.connection.execute_count,
            len(fake_redis.mget_requests),
            sum(
                execution["phase"] == "online_refresh"
                for execution in fake_redis.pipeline_executions
            ),
            sum(
                execution["phase"] == "desired_set_publication"
                for execution in fake_redis.pipeline_executions
            ),
        )
        assert actual == expected


class TestDesiredSetChurnAccounting:
    """T027 -- offline proof of accounting, not the 24-hour NFR-007 result."""

    @staticmethod
    def run_poll(service, *, join=4, leave=4):
        return TestBatchedPollOrchestration.run_poll(
            service, join=join, leave=leave
        )

    def test_metric_is_an_unlabelled_counter_separate_from_active_streams(self):
        metric = getattr(stream_monitoring_service, "desired_set_churn_total")

        assert metric._type == "counter"
        assert tuple(metric._labelnames) == ()
        assert metric is not stream_monitoring_service.active_stream_count
        assert stream_monitoring_service.active_stream_count._type == "gauge"

    def test_success_counts_entered_plus_departed_after_publication(self):
        fake_redis = FakeRedis()
        seed_desired(
            fake_redis,
            [(f"old-{index}", index) for index in range(1, 5)],
        )
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[
                (f"new-{index}", 100 + index) for index in range(1, 5)
            ],
        )
        metric = getattr(stream_monitoring_service, "desired_set_churn_total")
        before = metric._value.get()
        active_before = stream_monitoring_service.active_stream_count._value.get()
        values_at_publish = []
        publish = service.desired_store.publish

        def publish_and_observe(desired, broadcaster_ids):
            values_at_publish.append(metric._value.get())
            return publish(desired, broadcaster_ids)

        service.desired_store.publish = MagicMock(
            side_effect=publish_and_observe
        )

        self.run_poll(service)

        assert values_at_publish == [before], (
            "churn was counted before the desired-set publication succeeded"
        )
        assert metric._value.get() - before == 8
        assert service.last_poll_result["entered"] == 4
        assert service.last_poll_result["left"] == 4
        assert (
            service.last_poll_result["entered"]
            + service.last_poll_result["left"]
            == 8
        ), "8 changes per poll is directly observable; this is not 24-hour evidence"
        assert (
            stream_monitoring_service.active_stream_count._value.get()
            == active_before
        )

    @pytest.mark.parametrize("after_apply", [False, True])
    def test_failed_publication_does_not_count_churn(self, after_apply):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("old", 1)])
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("new", 2)],
            desired_publish_error=ConnectionError("publish failed"),
            desired_publish_error_after_apply=after_apply,
        )
        metric = getattr(stream_monitoring_service, "desired_set_churn_total")
        before = metric._value.get()

        self.run_poll(service, join=1, leave=1)

        assert service.last_poll_result["outcome"] == "desired_publish_failed"
        assert metric._value.get() == before


class TestPollObservabilityAndBoundaries:
    @staticmethod
    def run_poll(service):
        with patch.object(stream_monitoring_service, "JOIN_THRESHOLD", 1), \
             patch.object(stream_monitoring_service, "LEAVE_THRESHOLD", 1), \
             patch.object(
                 stream_monitoring_service,
                 "CLIPPING_DISABLED_FETCH_PAD_FRACTION",
                 0.0,
             ):
            return asyncio.run(service.poll_top_streams())

    @staticmethod
    def histogram_count(metric, **labels):
        sample_name = f"{metric._name}_count"
        for family in metric.collect():
            for sample in family.samples:
                if sample.name == sample_name and sample.labels == labels:
                    return sample.value
        return 0

    def assert_one_total_outcome(self, service, expected, run):
        metric = stream_monitoring_service.stream_poll_duration_seconds
        before = {
            outcome: self.histogram_count(metric, outcome=outcome)
            for outcome in stream_monitoring_service.POLL_OUTCOMES
        }

        run()

        deltas = {
            outcome: (
                self.histogram_count(metric, outcome=outcome)
                - before[outcome]
            )
            for outcome in stream_monitoring_service.POLL_OUTCOMES
        }
        assert deltas[expected] == 1
        assert sum(deltas.values()) == 1
        assert service.test_side_effects.final_outcomes == [expected]

    def test_every_completion_path_emits_exactly_one_bounded_total_outcome(self):
        scenarios = []

        fake_redis = FakeRedis()
        scenarios.append(("success", make_poller(["one"], fake_redis), None))

        fake_redis = FakeRedis()
        scenarios.append((
            "metadata_failed",
            make_poller(
                ["one"],
                fake_redis,
                pool=CountingPool(
                    CountingConnection(execute_error=ValueError("poison"))
                ),
            ),
            None,
        ))

        fake_redis = FakeRedis()
        scenarios.append((
            "ranking_failed",
            make_poller(
                [],
                fake_redis,
                twitch_error=RuntimeError("ranking"),
            ),
            None,
        ))

        fake_redis = FakeRedis()
        scenarios.append((
            "desired_read_failed",
            make_poller(
                ["one"],
                fake_redis,
                desired_read_error=ConnectionError("desired"),
            ),
            None,
        ))

        fake_redis = FakeRedis()
        fake_redis.fail_on = {"mget"}
        scenarios.append((
            "online_snapshot_failed",
            make_poller(["one"], fake_redis),
            None,
        ))

        fake_redis = FakeRedis()
        fake_redis.inject_pipeline_failure("online_refresh", when="before")
        scenarios.append((
            "online_refresh_failed",
            make_poller(["one"], fake_redis),
            None,
        ))

        fake_redis = FakeRedis()
        scenarios.append((
            "desired_publish_failed",
            make_poller(
                ["one"],
                fake_redis,
                desired_publish_error=ConnectionError("publish"),
            ),
            None,
        ))

        fake_redis = FakeRedis()
        scenarios.append((
            "unexpected_failure",
            make_poller(["one"], fake_redis),
            patch.object(
                stream_monitoring_service,
                "compute_desired_set",
                side_effect=RuntimeError("unexpected"),
            ),
        ))

        for expected, service, context in scenarios:
            if context is None:
                run = lambda service=service: self.run_poll(service)
                self.assert_one_total_outcome(service, expected, run)
            else:
                with context:
                    self.assert_one_total_outcome(
                        service,
                        expected,
                        lambda service=service: self.run_poll(service),
                    )

    def test_phase_metric_uses_only_bounded_phase_and_outcome_labels(self):
        fake_redis = FakeRedis()
        service = make_poller(["one"], fake_redis)
        self.run_poll(service)

        metric = stream_monitoring_service.stream_poll_phase_duration_seconds
        observed = {
            (
                sample.labels["phase"],
                sample.labels["outcome"],
                frozenset(sample.labels),
            )
            for family in metric.collect()
            for sample in family.samples
            if sample.name.endswith("_count")
        }
        assert {
            phase for phase, _, _ in observed
        } == set(stream_monitoring_service.POLL_PHASES)
        assert {
            outcome for _, outcome, _ in observed
        } <= set(stream_monitoring_service.PHASE_OUTCOMES)
        assert all(labels == {"phase", "outcome"} for _, _, labels in observed)

        empty = make_poller([], FakeRedis())
        self.run_poll(empty)
        assert {
            ("metadata_persistence", "empty"),
            ("online_snapshot", "empty"),
            ("online_refresh", "empty"),
            ("lifecycle_publication", "empty"),
        } <= set(empty.test_side_effects.phase_boundaries)

    def test_final_structured_log_carries_bounded_context(self, caplog):
        fake_redis = FakeRedis()
        service = make_poller(
            ["one"],
            fake_redis,
            pool=CountingPool(
                CountingConnection(execute_error=ValueError("poison"))
            ),
        )

        with caplog.at_level(logging.INFO):
            self.run_poll(service)

        final_records = [
            record
            for record in caplog.records
            if record.message == "Poll finished"
        ]
        assert len(final_records) == 1
        record = final_records[0]
        assert record.outcome == "metadata_failed"
        assert record.failed_phase == "metadata_persistence"
        assert set(record.phase_durations_seconds) == set(
            stream_monitoring_service.POLL_PHASES
        )
        assert record.ranked == 1
        assert record.desired == 1
        assert record.entered == 1
        assert record.left == 0
        assert record.metadata_input_count == 1
        assert record.metadata_unique_count == 1
        assert record.metadata_failure_streak == 1

    def test_production_compose_matches_feature_007_capacity_ceiling(self):
        repository_root = Path(__file__).resolve().parents[2]
        compose = (repository_root / "docker-compose.yml").read_text()
        stream_monitoring = compose.split("\n  stream-monitoring:", 1)[1]
        stream_monitoring = stream_monitoring.split("\n  api-frontend:", 1)[0]

        assert stream_monitoring.count("- JOIN_THRESHOLD=400") == 1
        assert stream_monitoring.count("- LEAVE_THRESHOLD=400") == 1
        assert (
            stream_monitoring.count(
                "- AUXILIARY_REFUSAL_RETRY_SECONDS=3600"
            )
            == 1
        )
        lines = stream_monitoring.splitlines()
        join_line = lines.index("      - JOIN_THRESHOLD=400")
        rationale = "\n".join(
            lines[max(0, join_line - 20):join_line + 6]
        ).lower()
        assert "400 channels" in rationale
        assert "800 subscriptions" in rationale
        assert "100" in rationale and "headroom" in rationale
        assert (
            stream_monitoring.count(
                "- CLIPPING_DISABLED_FETCH_PAD_FRACTION=0.30"
            )
            == 1
        )

    def test_validation_modules_are_not_in_production_copy_or_bind_mounts(self):
        repository_root = Path(__file__).resolve().parents[2]
        dockerfile = (
            repository_root
            / "services"
            / "stream-monitoring"
            / "Dockerfile"
        ).read_text()
        compose = (repository_root / "docker-compose.yml").read_text()

        for filename in (
            "feature006_driver.py",
            "feature006_fixtures.py",
        ):
            assert filename not in dockerfile
            assert filename not in compose


class TestReconcilerDiff:
    """T014a -- the diff, adoption, revocation, and mid-pass changes."""

    def test_creates_everything_missing(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 2, 3]
        assert reconciler.subscription_count == 3

    def test_works_highest_rank_first(self):
        """Rank order matters at cold start: the busiest channels come up
        first, so coverage is useful before the ramp finishes."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("top", 1), ("middle", 2), ("bottom", 3)])
        transport = StubTransport(latency_seconds=0.001)
        reconciler = make_reconciler(transport, fake_redis, concurrency=1)

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [1, 2, 3]

    def test_drops_what_is_no_longer_wanted(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        asyncio.run(reconciler.reconcile_once())

        seed_desired(fake_redis, [("a", 1)])
        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1]
        assert len(transport.delete_calls) == 1

    def test_adopts_an_existing_subscription_instead_of_recreating_it(self):
        """FR-005. A restart must not duplicate what is already there."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        transport = StubTransport()
        asyncio.run(transport.create(1))  # already subscribed before we start
        transport.create_calls.clear()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [2], "channel 1 was re-created, not adopted"
        assert len(transport.subscriptions) == 2

    def test_revoked_subscription_is_recreated(self):
        """T014. A revoked subscription is not live, so it counts as absent."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        asyncio.run(transport.create(1))
        transport.revoke(1)
        transport.create_calls.clear()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [1]
        assert transport.statuses[1] == "enabled"

    def test_channel_that_leaves_mid_pass_is_not_created(self):
        """T014. A poll landing during a long cold ramp must be picked up
        through the generation counter, not acted on a pass too late."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])

        class ShrinkingTransport(StubTransport):
            """The poller lands right after the first create and shrinks the set."""

            reconciler = None

            async def create(self, broadcaster_id):
                result = await super().create(broadcaster_id)
                if len(self.create_calls) == 1:
                    seed_desired(fake_redis, [("a", 1)])
                    self.reconciler.notify_desired_changed()
                return result

        transport = ShrinkingTransport(latency_seconds=0.001)
        reconciler = make_reconciler(transport, fake_redis, concurrency=1)
        transport.reconciler = reconciler

        asyncio.run(reconciler.reconcile_once())

        assert transport.create_calls == [1], "kept creating channels nobody wants"
        assert reconciler.subscription_count == 1

    def test_converges_from_empty_partial_and_drifted(self):
        """FR-005 -- the same pass has to work from any starting state."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())            # from empty
        assert sorted(transport.subscriptions) == [1, 2, 3]

        transport.revoke(2)                                  # drift
        transport.subscriptions.pop(3)                       # socket death
        reconciler._adoption_complete = False                # forces re-enumeration
        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 2, 3]
        assert all(status == "enabled" for status in transport.statuses.values())

    def test_desired_login_without_an_id_is_skipped_not_crashed(self):
        """The poller writes both keys in one transaction, so this should not
        happen. If it ever does, skip the login rather than guess an id."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        del fake_redis.hashes[DESIRED_IDS_KEY]["b"]
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1]


class TestReconcilerRateLimit:
    """D2 -- the 429 retry loop is load-bearing past about 400 channels."""

    def test_rate_limited_channels_are_retried_never_dropped(self):
        fake_redis = FakeRedis()
        channels = [(f"s{i}", i) for i in range(1, 201)]
        seed_desired(fake_redis, channels)
        # A burst budget like the measured one: creates succeed until it runs
        # out, then 429 until the backoff refills it.
        transport = StubTransport(burst_budget=50, budget_refill_seconds=0.0)
        reconciler = make_reconciler(transport, fake_redis)
        before = counter_value("rate_limited")

        asyncio.run(reconciler.reconcile_once())

        assert len(transport.subscriptions) == 200, "channels were dropped on a 429"
        assert counter_value("rate_limited") > before, "429s were not counted"

    def test_refusal_is_counted_and_does_not_block_the_rest(self):
        """About 1.5% of channels refuse. One refusal must not stall the ramp."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])
        transport = StubTransport(refuse={2})
        reconciler = make_reconciler(transport, fake_redis)
        before = counter_value("refused")

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 3]
        assert counter_value("refused") == before + 1

    def test_backoff_honours_the_retry_after_the_transport_offers(self):
        reconciler = make_reconciler(StubTransport(), FakeRedis())

        backoff = reconciler._backoff_for([(1, RateLimitedError(retry_after=7.0))])

        assert 7.0 <= backoff <= 7.7


class TestReconcilerAdoption:
    """T013 and T014b -- rebuilding the actual set, including a partial read."""

    def test_partial_enumeration_keeps_what_it_saw(self):
        """NFR-003. Pagination raising part way must not lose the entries
        already read, and must not present the unread ones as absent."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        transport.list_fails_after = 2
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert reconciler.subscription_count == 2, "the partial read was thrown away"
        assert reconciler._adoption_complete is False

    def test_a_loss_during_enumeration_is_not_swallowed_by_the_completion(self):
        """`transport.list()` has awaits in it and the pool's supervisor runs
        on the same loop, so a socket can die *while* the actual set is being
        rebuilt. Marking the adoption complete afterwards threw that signal
        away: the walk's snapshot still held the dead session, so its channels
        stayed recorded as covered while nothing delivered for them, and
        nothing re-enumerated until some later, unrelated loss."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        reconciler = make_reconciler(transport, fake_redis)

        # A connection dies part way through the walk, exactly as the pool's
        # supervisor would report it.
        original_list = transport.list

        def list_with_a_loss_midway():
            async def wrapped():
                index = 0
                async for subscription in original_list():
                    if index == 1:
                        reconciler.invalidate_actual_set()
                    index += 1
                    yield subscription
            return wrapped()

        transport.list = list_with_a_loss_midway
        asyncio.run(reconciler.reconcile_once())

        assert reconciler._adoption_complete is False, (
            "the completion overwrote the invalidation, so nothing will "
            "re-enumerate and the lost channels stay recorded as covered"
        )

    def test_the_actual_set_is_re_enumerated_periodically(self):
        """Adoption used to run once and then never again unless the pool
        observed a dead socket or a revocation. A subscription lost by any
        route the pool cannot see -- the library's `_resubscribe()` failing
        part way through a reconnect is the known one -- was therefore
        permanent, and `eventsub_subscription_count` went on counting it, so
        the FR-012 alert could not fire."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, readopt_interval_seconds=3600)
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is True

        # Twitch loses the subscription behind the pool's back.
        transport.subscriptions.clear()
        transport.statuses.clear()

        # Not due yet: the stale view survives, which is the cheap steady state.
        asyncio.run(reconciler.reconcile_once())

        # Due now.
        reconciler._last_adopt = float("-inf")
        asyncio.run(reconciler.reconcile_once())
        assert 1 in reconciler._actual, "the channel was not re-created"
        assert transport.create_calls.count(1) == 2, (
            "the re-enumeration did not notice the subscription had gone"
        )

    def test_a_failed_enumeration_backs_off(self):
        """A walk that raised will probably raise again in 5 s, and retrying it
        every pass is a full multi-page Helix enumeration ~12 times a minute on
        the token the clip job shares.

        Stamping `_last_adopt` does NOT bound this, though an earlier comment
        here claimed it did: a partial walk also clears `_adoption_complete`,
        and `reconcile_once` re-adopts on `not complete OR readopt_due`, so the
        first disjunct fires however recently `_last_adopt` was stamped. The
        earlier version of this test could not tell the difference -- it only
        checked `_readopt_due()` after a SUCCESSFUL recovery walk, which stamps
        `_last_adopt` either way.
        """
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3), ("d", 4)])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        reconciler = make_reconciler(
            transport, fake_redis, readopt_interval_seconds=3600, adopt_retry_seconds=30
        )
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is True

        # The periodic re-check comes due and Twitch fails mid-pagination.
        reconciler._last_adopt = float("-inf")
        transport.list_fails_after = 2
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is False
        walks_after_failure = len(transport.list_calls)

        # The next passes must NOT re-enumerate, even though the view is known
        # to be incomplete. That is the whole point of the backoff.
        asyncio.run(reconciler.reconcile_once())
        asyncio.run(reconciler.reconcile_once())
        assert len(transport.list_calls) == walks_after_failure, (
            "a failed enumeration is being retried on every pass"
        )

        # And once the window passes it tries again and recovers.
        reconciler._adopt_retry_after = float("-inf")
        transport.list_fails_after = None
        asyncio.run(reconciler.reconcile_once())
        assert reconciler._adoption_complete is True
        assert reconciler._readopt_due() is False

    def test_a_socket_loss_is_not_delayed_by_the_enumeration_backoff(self):
        """The two failures are positively correlated -- one network blip both
        fails a walk and kills a socket -- so this pairing is likely, not
        exotic. While the backoff held, `_actual` still carried the dead
        socket's ids, so its channels never entered `to_create` and stayed dark
        for the full window.

        The earlier version of this test set no backoff at all, so it passed
        whether or not the loss path was gated. This one arms it first.
        """
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, adopt_retry_seconds=30)
        asyncio.run(reconciler.reconcile_once())
        walks = len(transport.list_calls)

        # A failed walk arms the backoff, then the socket dies a second later.
        reconciler._adopt_retry_after = time.monotonic() + 30
        reconciler.invalidate_actual_set()
        asyncio.run(reconciler.reconcile_once())

        assert len(transport.list_calls) > walks, (
            "a socket loss waited out the failed-enumeration backoff, so up to "
            "300 channels stay dark for the whole window"
        )
        assert reconciler._adoption_complete is True

    def test_partial_enumeration_never_deletes(self):
        """The dangerous move is deleting on an incomplete picture: an unseen
        subscription is not an unwanted one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])  # nothing is wanted -- everything looks droppable
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        transport.list_fails_after = 2
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert transport.delete_calls == [], "deleted on an incomplete view"
        assert len(transport.subscriptions) == 4

    def test_enumeration_is_retried_until_one_succeeds(self):
        """Once the view is whole, the held-back drops go through."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        for broadcaster_id in (1, 2, 3, 4):
            asyncio.run(transport.create(broadcaster_id))
        transport.list_fails_after = 2
        # No failure backoff here: this test is about the retry converging,
        # and `test_a_failed_enumeration_backs_off` covers the pacing.
        reconciler = make_reconciler(transport, fake_redis, adopt_retry_seconds=0)
        asyncio.run(reconciler.reconcile_once())

        transport.list_fails_after = None
        asyncio.run(reconciler.reconcile_once())

        assert reconciler._adoption_complete is True
        assert transport.subscriptions == {}
        assert len(transport.delete_calls) == 4

    def test_only_enabled_subscriptions_are_adopted(self):
        """A lingering `websocket_disconnected` entry is not a live one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [])
        transport = StubTransport()
        asyncio.run(transport.create(1))
        asyncio.run(transport.create(2))
        transport.statuses[2] = "websocket_disconnected"
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler._adopt())

        assert reconciler.subscription_count == 1


class TestReconcilerResilience:
    """T014c -- NFR-002. A Redis fault is a skipped pass, not a dead service."""

    def test_redis_failure_skips_the_pass_without_crashing(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        fake_redis.fail_on = {"zrange"}

        asyncio.run(reconciler.reconcile_once())  # must not raise

        assert transport.create_calls == []

    def test_redis_failure_drops_no_live_subscription(self):
        """The failure path must never look like "nothing is wanted"."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        asyncio.run(reconciler.reconcile_once())
        assert len(transport.subscriptions) == 2

        fake_redis.fail_on = {"zrange"}
        asyncio.run(reconciler.reconcile_once())

        assert len(transport.subscriptions) == 2, "a Redis fault dropped live work"
        assert transport.delete_calls == []

    def test_the_next_pass_recovers(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)
        fake_redis.fail_on = {"zrange"}
        asyncio.run(reconciler.reconcile_once())

        fake_redis.fail_on = set()
        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1]

    def test_a_transport_failure_does_not_stop_the_other_channels(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])

        class FlakyTransport(StubTransport):
            async def create(self, broadcaster_id):
                if broadcaster_id == 2:
                    raise TransportError("boom")
                return await super().create(broadcaster_id)

        transport = FlakyTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.subscriptions) == [1, 3]

    def test_a_rotating_session_is_counted_apart_and_retried_next_pass(self):
        """The 2026-08-30 ramp: a cold start throws a burst of these as the
        first session reconnects. They are not real failures -- the next pass
        recreates the channel -- so they must not land in the "error" count."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])

        class RotatingOnce(StubTransport):
            def __init__(self):
                super().__init__()
                self._rotated = False

            async def create(self, broadcaster_id):
                if broadcaster_id == 2 and not self._rotated:
                    self._rotated = True
                    raise TransientSessionError(
                        "websocket transport session does not exist"
                    )
                return await super().create(broadcaster_id)

        transport = RotatingOnce()
        reconciler = make_reconciler(transport, fake_redis)

        before_transient = counter_value("transient_session")
        before_error = counter_value("error")
        asyncio.run(reconciler.reconcile_once())
        assert sorted(transport.subscriptions) == [1]
        assert counter_value("transient_session") == before_transient + 1
        assert counter_value("error") == before_error

        # Still wanted, so the next pass creates it.
        asyncio.run(reconciler.reconcile_once())
        assert sorted(transport.subscriptions) == [1, 2]

    def test_the_loop_keeps_running_across_a_failing_pass(self):
        """The asyncio task must outlive a bad pass, or a stalled reconciler
        would look exactly like a healthy one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, idle_timeout_seconds=0.01)
        fake_redis.fail_on = {"zrange"}

        async def run_briefly():
            task = asyncio.create_task(reconciler.run())
            await asyncio.sleep(0.05)
            fake_redis.fail_on = set()
            await asyncio.sleep(0.05)
            reconciler.stop()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run_briefly())

        assert sorted(transport.subscriptions) == [1], "the loop did not recover"


class TestReconcilerConcurrencyBound:
    """T014d -- NFR-001. No task or thread per channel."""

    def test_task_count_stays_bounded_at_500_channels(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"s{i}", i) for i in range(1, 501)])

        class TaskCountingTransport(StubTransport):
            max_tasks = 0

            async def create(self, broadcaster_id):
                TaskCountingTransport.max_tasks = max(
                    TaskCountingTransport.max_tasks, len(asyncio.all_tasks())
                )
                return await super().create(broadcaster_id)

        transport = TaskCountingTransport(latency_seconds=0.0005)
        reconciler = make_reconciler(transport, fake_redis, concurrency=10)

        asyncio.run(reconciler.reconcile_once())

        assert len(transport.subscriptions) == 500
        assert TaskCountingTransport.max_tasks <= 10 + 5, (
            f"{TaskCountingTransport.max_tasks} tasks alive for 500 channels -- "
            "the reconciler is spawning per-channel work"
        )

    def test_worker_pool_never_exceeds_the_configured_concurrency(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"s{i}", i) for i in range(1, 101)])

        class ConcurrencyProbe(StubTransport):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.in_flight = 0
                self.peak = 0

            async def create(self, broadcaster_id):
                self.in_flight += 1
                self.peak = max(self.peak, self.in_flight)
                try:
                    return await super().create(broadcaster_id)
                finally:
                    self.in_flight -= 1

        transport = ConcurrencyProbe(latency_seconds=0.001)
        reconciler = make_reconciler(transport, fake_redis, concurrency=4)

        asyncio.run(reconciler.reconcile_once())

        assert transport.peak <= 4


class TestRefusalStoreFaults:
    """A failed refusal READ must not read as "nothing is refused"."""

    def test_a_database_fault_reaches_the_caller(self):
        """Returning {} for a failed read is a wrong answer, not a degraded
        one, and it silently disabled `_drop_refused`'s own handling -- both
        its except branch and its "Refusal cache unavailable" log were
        unreachable for the real store. The symptom was every refused channel
        being retried every pass, one POST each, with nothing in the log."""
        pool = MagicMock()
        pool.getconn.side_effect = RuntimeError("postgres is away")
        store = reconciler_module.PostgresRefusalStore(pool)

        with pytest.raises(RuntimeError):
            store.refusals([1, 2, 3])

    def test_a_write_fault_is_still_swallowed(self):
        """The writes keep their old behaviour: a refusal that fails to record
        is re-learned next pass, which is harmless."""
        pool = MagicMock()
        pool.getconn.side_effect = RuntimeError("postgres is away")
        store = reconciler_module.PostgresRefusalStore(pool)

        store.mark_refused(1)
        store.clear_refusal(1)

    def test_the_reconciler_attempts_every_channel_when_the_store_faults(self):
        """And the caller's handler, now reachable, does the safe thing."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        store = MagicMock()
        store.refusals.side_effect = RuntimeError("postgres is away")
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis, refusal_store=store)

        asyncio.run(reconciler.reconcile_once())

        assert sorted(transport.create_calls) == [1, 2], (
            "a store fault stopped the reconciler subscribing"
        )


class TestReconcilerMetrics:
    """T015 and T016 -- FR-012. A stalled reconciler must be visible while the
    polls keep succeeding (US2 acceptance scenario 2)."""

    class ReportingTransport(StubTransport):
        def __init__(self, occupancy, coverage=None):
            super().__init__()
            self.reported_occupancy = dict(occupancy)
            self.reported_coverage = dict(coverage or {})

        def occupancy(self):
            return dict(self.reported_occupancy)

        def coverage_counts(self):
            return dict(self.reported_coverage)

    @pytest.fixture(autouse=True)
    def reset_reconciler_gauges(self):
        reconciler_module.eventsub_subscription_count.set(0)
        reconciler_module.eventsub_connection_occupancy.clear()
        stream_monitoring_service.active_stream_count.set(0)
        coverage_metric = getattr(
            reconciler_module, "eventsub_channel_coverage", None
        )
        if coverage_metric is not None:
            coverage_metric.clear()
        yield
        reconciler_module.eventsub_subscription_count.set(0)
        reconciler_module.eventsub_connection_occupancy.clear()
        stream_monitoring_service.active_stream_count.set(0)
        if coverage_metric is not None:
            coverage_metric.clear()

    @pytest.mark.parametrize(
        ("channels", "coverage_state", "occupancy", "expected_subscriptions"),
        [
            (1, "complete", {"connection-0": 2}, 2),
            (1, "chat_only", {"connection-0": 1}, 1),
            (1, "notification_only", {"connection-0": 1}, 1),
            (
                400,
                "complete",
                {
                    "connection-0": 300,
                    "connection-1": 300,
                    "connection-2": 200,
                },
                800,
            ),
        ],
        ids=[
            "complete",
            "chat-only",
            "notification-only",
            "four-hundred-complete",
        ],
    )
    def test_subscription_count_is_the_sum_of_transport_occupancy(
        self, channels, coverage_state, occupancy, expected_subscriptions
    ):
        """FR-015: this gauge counts subscription slots, never channels."""
        coverage = {
            "complete": 0,
            "chat_only": 0,
            "notification_only": 0,
            "degraded_chat_only": 0,
        }
        coverage[coverage_state] = channels
        transport = self.ReportingTransport(occupancy, coverage)
        reconciler = make_reconciler(transport, FakeRedis())
        reconciler._actual = {
            broadcaster_id: f"chat-{broadcaster_id}"
            for broadcaster_id in range(channels)
        }

        reconciler._publish_subscription_count()

        assert (
            reconciler_module.eventsub_subscription_count._value.get()
            == expected_subscriptions
        )

    def test_subscription_count_tracks_the_transport_not_the_actual_set(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1), ("b", 2)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.eventsub_subscription_count._value.get() == 2

    def test_the_subscription_count_drops_when_a_socket_takes_its_channels(self):
        """The FR-012 alert is the DIP, and there was no dip to alert on.

        The gauge was written once, at the end of a pass. A socket loss left
        `_actual` untouched, so the next pass re-created everything and set the
        gauge from the old value back to the same value -- no scrape in between
        could ever see it move. And if that pass's enumeration failed, which a
        blip that kills a socket is exactly what does, `_adopt` merged the
        stale entries back and the healthy-looking count survived pass after
        pass while those channels were dark.
        """
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())
        assert reconciler_module.eventsub_subscription_count._value.get() == 5

        # The socket went, and took three of them with it.
        for broadcaster_id in (1, 2, 3):
            transport.subscriptions.pop(broadcaster_id)
            transport.statuses.pop(broadcaster_id)
        reconciler.invalidate_actual_set(3)

        assert reconciler_module.eventsub_subscription_count._value.get() == 2, (
            "the gauge went on reporting subscriptions Twitch no longer has"
        )

    def test_the_subscription_count_climbs_during_a_ramp(self):
        """A cold start can run for tens of seconds, and a rate-limited one for
        minutes. The runbook tells the operator to check whether the count is
        still climbing before restarting anything, so it has to actually climb
        rather than appear all at once when the pass ends."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        seen = []

        class WatchedTransport(StubTransport):
            async def create(self, broadcaster_id):
                subscription_id = await super().create(broadcaster_id)
                seen.append(reconciler_module.eventsub_subscription_count._value.get())
                return subscription_id

        reconciler = make_reconciler(WatchedTransport(), fake_redis, concurrency=1)
        reconciler_module.eventsub_subscription_count.set(0)

        asyncio.run(reconciler.reconcile_once())

        assert seen == sorted(seen) and seen[-1] > seen[0], (
            f"the gauge did not move during the ramp (samples {seen})"
        )

    def test_repeated_losses_between_passes_accumulate(self):
        """Several revocations can land before the next enumeration. Deriving
        the dip from `len(_actual)` each time reported the SAME value for all
        of them -- the set does not shrink on a loss, because the transport
        says how many went, not which -- so three losses looked like one."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())
        for broadcaster_id in (1, 2, 3):
            transport.subscriptions.pop(broadcaster_id)
            transport.statuses.pop(broadcaster_id)
            reconciler.invalidate_actual_set(1)

        assert reconciler_module.eventsub_subscription_count._value.get() == 2

    def test_a_failed_walk_does_not_re_inflate_the_count(self):
        """`_adopt` merges what it saw into what it had when a walk fails, so
        the lost subscriptions are still in `_actual`. Publishing
        `len(_actual)` there would undo the dip on the very next pass and
        restore the healthy-looking count for as long as the walks keep
        failing -- which is the state this alert exists to catch."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [(f"c{i}", i) for i in range(1, 6)])
        transport = StubTransport()
        reconciler = make_reconciler(transport, fake_redis)

        asyncio.run(reconciler.reconcile_once())
        for broadcaster_id in (1, 2, 3):
            transport.subscriptions.pop(broadcaster_id)
            transport.statuses.pop(broadcaster_id)
        reconciler.invalidate_actual_set(3)
        transport.list_fails_after = 0
        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.eventsub_subscription_count._value.get() == 2, (
            "a failed enumeration restored the count the loss had corrected"
        )
        # And a clean walk settles it exactly, once the failed walk's backoff
        # has expired.
        transport.list_fails_after = None
        reconciler._adopt_retry_after = float("-inf")
        asyncio.run(reconciler.reconcile_once())
        assert reconciler_module.eventsub_subscription_count._value.get() == 5

    def test_last_success_timestamp_advances_on_a_good_pass(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.reconcile_last_success_timestamp._value.get() > 0

    def test_last_success_timestamp_stalls_when_the_pass_fails(self):
        """This is the signal that separates "reconciler dead" from "poll
        dead". It must not move on a pass that could not read the intent."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)
        asyncio.run(reconciler.reconcile_once())
        stalled_at = reconciler_module.reconcile_last_success_timestamp._value.get()

        fake_redis.fail_on = {"zrange"}
        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.reconcile_last_success_timestamp._value.get() == stalled_at

    def test_a_mid_pass_refresh_does_not_swallow_an_invalidation(self):
        """The two signals -- "the poller wrote a new set" and "a socket died"
        -- have their own events. Sharing one meant each fix for the other
        broke something: clearing it in the refresh swallowed the socket loss,
        and re-setting it there left it set for the rest of the pass, so every
        remaining channel re-read Redis three times over."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)

        async def run():
            # Both signals land while a pass is in flight.
            reconciler.notify_desired_changed()
            reconciler.invalidate_actual_set()
            await reconciler._maybe_refresh_desired()

            assert reconciler._invalidated.is_set(), (
                "the desired-set refresh consumed the socket-loss wake-up"
            )
            # And the desired-set signal really is consumed, so the rest of the
            # pass does not re-read Redis per channel.
            assert not reconciler._wake.is_set()
            calls = len(fake_redis.calls)
            await reconciler._maybe_refresh_desired()
            assert len(fake_redis.calls) == calls, (
                "the refresh ran again for the next channel"
            )

        asyncio.run(run())

    def test_connection_occupancy_is_published(self):
        transport = self.ReportingTransport(
            {"retired": 3, "survivor": 2}
        )
        reconciler = make_reconciler(transport, FakeRedis())

        asyncio.run(reconciler.reconcile_once())

        assert gauge_label_values(
            reconciler_module.eventsub_connection_occupancy, "connection"
        ) == {"retired": 3, "survivor": 2}

        transport.reported_occupancy = {"survivor": 1, "replacement": 4}
        asyncio.run(reconciler.reconcile_once())

        assert gauge_label_values(
            reconciler_module.eventsub_connection_occupancy, "connection"
        ) == {"survivor": 1, "replacement": 4}, (
            "a retired connection retained its stale occupancy label"
        )

    def test_channel_coverage_is_bounded_channel_state_accounting(self):
        transport = self.ReportingTransport(
            {"connection-0": 5},
            {
                "complete": 1,
                "chat_only": 1,
                "notification_only": 1,
                "degraded_chat_only": 1,
            },
        )
        reconciler = make_reconciler(transport, FakeRedis())

        asyncio.run(reconciler.reconcile_once())

        coverage_metric = getattr(
            reconciler_module, "eventsub_channel_coverage", None
        )
        assert coverage_metric is not None
        assert tuple(coverage_metric._labelnames) == ("state",)
        assert gauge_label_values(coverage_metric, "state") == {
            "complete": 1,
            "chat_only": 1,
            "notification_only": 1,
            "degraded_chat_only": 1,
        }

        transport.reported_coverage = {
            "complete": 0,
            "chat_only": 0,
            "notification_only": 2,
            "degraded_chat_only": 0,
        }
        asyncio.run(reconciler.reconcile_once())

        assert gauge_label_values(coverage_metric, "state") == {
            "complete": 0,
            "chat_only": 0,
            "notification_only": 2,
            "degraded_chat_only": 0,
        }, "coverage states were not deterministically replaced"

    def test_loss_republishes_live_transport_occupancy_immediately(self):
        """The loss dip remains visible, but no channel/subscription arithmetic."""
        transport = self.ReportingTransport({"connection-0": 4})
        reconciler = make_reconciler(transport, FakeRedis())
        reconciler._actual = {1: "chat-1", 2: "chat-2"}
        reconciler._publish_subscription_count()
        assert reconciler_module.eventsub_subscription_count._value.get() == 4

        transport.reported_occupancy = {"connection-0": 1}
        reconciler.invalidate_actual_set(3)

        assert reconciler_module.eventsub_subscription_count._value.get() == 1

    def test_reconcile_duration_is_observed(self):
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        reconciler = make_reconciler(StubTransport(), fake_redis)
        before = reconciler_module.reconcile_duration_seconds._sum.get()

        asyncio.run(reconciler.reconcile_once())

        assert reconciler_module.reconcile_duration_seconds._sum.get() >= before

    def test_active_stream_count_remains_channel_based_with_dual_coverage(self):
        """joined_channels is no longer maintained, so the old gauge would sit
        at zero forever. It follows actual channels, not subscription slots."""
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        on_pass_complete = MagicMock(
            side_effect=stream_monitoring_service.active_stream_count.set
        )
        transport = self.ReportingTransport(
            {"connection-0": 2},
            {
                "complete": 1,
                "chat_only": 0,
                "notification_only": 0,
                "degraded_chat_only": 0,
            },
        )
        reconciler = Reconciler(
            transport=transport,
            desired_store=RedisDesiredSetStore(fake_redis),
            config=ReconcilerConfig(concurrency=4, idle_timeout_seconds=0.01),
            on_pass_complete=on_pass_complete,
        )

        asyncio.run(reconciler.reconcile_once())

        on_pass_complete.assert_called_once_with(1)
        assert stream_monitoring_service.active_stream_count._value.get() == 1
        assert reconciler_module.eventsub_subscription_count._value.get() == 2


class TestReconcilerLifecycle:
    """T012 -- the reconciler is a task in THIS process, started from start().

    Not a separate container: it shares the process /health endpoint and this
    logger. It must also be stopped before Redis closes underneath it.
    """

    def test_start_launches_the_reconciler_task(self):
        service = StreamMonitoringService()
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        transport = StubTransport()

        async def fake_initialize():
            service.redis_client = fake_redis
            service.scheduler = MagicMock()
            service.reconciler = make_reconciler(transport, fake_redis)

        async def run_briefly():
            service.initialize = fake_initialize
            # Let start() run for real and stop it the way a signal would.
            # Pre-setting `running = False` no longer works as a shortcut:
            # start() now treats that as "shutdown was signalled during
            # start-up" and deliberately launches nothing.
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.05)
            assert service._reconciler_task is not None
            assert not service._reconciler_task.done()
            service.running = False
            await starter
            task = service._reconciler_task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return task

        task = asyncio.run(run_briefly())

        assert sorted(transport.subscriptions) == [1], "the task never reconciled"
        assert task.cancelled() or task.done(), "the loop outlived its event loop"

    def test_shutdown_survives_a_scheduler_that_was_never_started(self):
        """`start()` can now return before `scheduler.start()` when shutdown is
        signalled during start-up. APScheduler's `shutdown()` on a never-started
        scheduler raises `AttributeError: 'NoneType' object has no attribute
        'call_soon_threadsafe'`, and that escaped `stop()` and abandoned every
        step after it: the reconciler never stopped, websockets stayed open,
        and the Kafka producer was never flushed, so buffered chat was dropped.
        The tests missed it because they all pass a MagicMock scheduler.
        """

        async def run():
            from apscheduler.schedulers.asyncio import AsyncIOScheduler

            service = StreamMonitoringService()
            service.scheduler = AsyncIOScheduler()   # real, and never started
            service._scheduler_started = False
            service.kafka_producer = MagicMock()
            service.redis_client = MagicMock()

            await service.stop()   # must not raise

            # Everything after the scheduler still ran.
            service.kafka_producer.flush.assert_called_once()
            service.redis_client.close.assert_called_once()

        asyncio.run(run())

    def test_a_signal_during_start_up_tears_down_a_whole_service(self):
        """A signal can land while `initialize()` is still building things --
        a `docker compose restart` during a slow Twitch auth does it. Tearing
        down then closed the aiohttp session the auth call was using, while
        every other branch no-opped on a Kafka producer, DB pool, Redis client
        and transport that did not exist yet. `initialize()` went on to build
        all of them and nothing ever closed them, after "stopped" was logged.
        """

        async def run():
            service = StreamMonitoringService()
            built = asyncio.Event()

            async def slow_initialize():
                await asyncio.sleep(0.1)          # the Twitch auth
                service.scheduler = MagicMock()
                service.redis_client = MagicMock()
                service.kafka_producer = MagicMock()
                service.reconciler = None
                built.set()

            service.initialize = slow_initialize

            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)             # SIGTERM lands mid-auth
            stopper = asyncio.create_task(service.stop())
            await asyncio.wait_for(starter, timeout=2)
            await asyncio.wait_for(stopper, timeout=2)

            assert built.is_set(), "initialize() did not finish"
            # The resources initialize() built were actually torn down.
            service.redis_client.close.assert_called_once()
            service.kafka_producer.flush.assert_called_once()

        asyncio.run(run())

    def test_stop_cancels_a_start_up_that_will_not_finish(self):
        """Waiting on start-up was not enough on its own.

        `stop()` bounds that wait so a Twitch auth that never returns cannot
        block SIGTERM for ever -- but timing out and tearing down anyway left
        `initialize()` running, so it went on to build a Kafka producer, a
        Postgres pool, a Redis client and live websockets AFTER the teardown
        had already decided none of them existed. `_stopping` is set by then,
        so no later `stop()` could reach them either: the process logged
        "stopped" and then allocated everything it had just promised to close.
        The bound has to cancel, not just give up waiting.
        """

        async def run():
            service = StreamMonitoringService()
            built_after_teardown = []

            async def never_finishing_initialize():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    raise
                built_after_teardown.append("kafka")

            service.initialize = never_finishing_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)

            with patch.object(stream_monitoring_service, "INITIALIZE_WAIT_SECONDS", 0.05):
                await asyncio.wait_for(service.stop(), timeout=2)

            await asyncio.wait_for(starter, timeout=2)
            assert service._init_task.cancelled(), (
                "start-up was left running after the teardown that gave up on it"
            )
            assert built_after_teardown == [], (
                "initialize() allocated resources after stop() had finished"
            )

        asyncio.run(run())

    def test_a_signal_before_start_up_stops_it_from_initializing(self):
        """The signal can also beat `start()` entirely -- `main()` awaits the
        health server first. `stop()` has nothing to wait on and nothing to
        close at that point, so it finishes immediately; `start()` must not
        then go on and build a service nobody will ever tear down."""

        async def run():
            service = StreamMonitoringService()
            initialized = []
            service.initialize = lambda: initialized.append(True)

            await service.stop()
            await service.start()

            assert initialized == []
            assert service._init_task is None

        asyncio.run(run())

    def test_a_cancellation_aimed_at_start_is_not_swallowed(self):
        """`start()` treats a `CancelledError` from awaiting the start-up task
        as "shutdown cancelled me". Only `stop()` does that, and it sets
        `_stopping` first -- any other cancellation is aimed at `start()`
        itself and passing through, so swallowing it would break cancellation
        for whoever asked for it."""

        async def run():
            service = StreamMonitoringService()

            async def slow_initialize():
                await asyncio.sleep(60)

            service.initialize = slow_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)
            starter.cancel()

            with pytest.raises(asyncio.CancelledError):
                await starter

        asyncio.run(run())

    def test_a_cancellation_during_shutdown_does_not_abandon_the_teardown(self):
        """`_stopping` said "a shutdown is running", not "the shutdown
        cancelled you". An outer runtime unwinding its tasks while a
        signal-driven `stop()` was already waiting cancelled `start()` from
        outside: `start()` swallowed it because `_stopping` was set, and the
        same cancellation came back through `stop()`'s shielded wait, where
        nothing caught it -- so every teardown step below was skipped and the
        producer was never flushed. Both sides now ask whether the
        cancellation was aimed at THEM."""

        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            service.redis_client = MagicMock()

            async def slow_initialize():
                await asyncio.sleep(60)

            service.initialize = slow_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.02)
            stopper = asyncio.create_task(service.stop())
            await asyncio.sleep(0.02)
            starter.cancel()                     # from outside, mid-shutdown

            with pytest.raises(asyncio.CancelledError):
                await starter
            await asyncio.wait_for(stopper, timeout=2)

            service.kafka_producer.flush.assert_called_once()
            service.redis_client.close.assert_called_once()

        asyncio.run(run())

    def test_stop_cancels_the_reconciler_before_closing_redis(self):
        service = StreamMonitoringService()
        fake_redis = FakeRedis()
        seed_desired(fake_redis, [("a", 1)])
        service.redis_client = MagicMock()
        service.reconciler = make_reconciler(StubTransport(), fake_redis)

        async def run_and_stop():
            service._reconciler_task = asyncio.create_task(service.reconciler.run())
            await asyncio.sleep(0.03)
            await service.stop()
            return service._reconciler_task

        task = asyncio.run(run_and_stop())

        assert task.done()
        assert service.reconciler.running is False
        service.redis_client.close.assert_called_once()


class TestReconcilerConfig:
    """Settings are validated up front, the way resolve_thresholds is."""

    def test_defaults_match_the_measured_decision(self):
        config = reconciler_module.resolve_reconciler_config({})
        assert config.concurrency == 10
        assert config.idle_timeout_seconds == 5.0
        assert config.rate_limit_backoff_seconds == 10.0

    def test_concurrency_is_read_from_the_environment(self):
        config = reconciler_module.resolve_reconciler_config({"RECONCILE_CONCURRENCY": "15"})
        assert config.concurrency == 15

    def test_zero_concurrency_is_rejected(self):
        """A pool of nothing would never converge, and would do it silently."""
        with pytest.raises(ValueError, match="RECONCILE_CONCURRENCY must be >= 1"):
            reconciler_module.resolve_reconciler_config({"RECONCILE_CONCURRENCY": "0"})

    def test_zero_idle_timeout_is_rejected(self):
        with pytest.raises(ValueError, match="RECONCILE_IDLE_TIMEOUT_SECONDS"):
            reconciler_module.resolve_reconciler_config({"RECONCILE_IDLE_TIMEOUT_SECONDS": "0"})

    def test_transport_interface_cannot_be_used_directly(self):
        """The seam is abstract on purpose: Phase 2 supplies the real pool."""
        with pytest.raises(TypeError):
            reconciler_module.SubscriptionTransport()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ---------------------------------------------------------------------------
# Phase 2 -- the EventSub transport
# ---------------------------------------------------------------------------


# The two EventSub coverage types, spelled out here rather than imported so a
# missing constant fails the Feature 007 tests alone instead of the whole
# module. `TestCoverageTypeContract` asserts the module agrees with them.
CHAT_TYPE = "channel.chat.message"
NOTIFICATION_TYPE = "channel.chat.notification"


class FakeTask:
    """One of the library's socket tasks. Done means the socket is finished."""

    def __init__(self, finished=False):
        self._finished = finished

    def done(self):
        return self._finished


class FakeWebsocket:
    """Enough EventSubWebsocket for the pool to drive.

    It mirrors the three private attributes the pool has to read on the real
    one: `_active_subscriptions` (to find an id a reconnect rotated),
    `_callbacks` (so a dropped channel is not resubscribed), and `_tasks`
    (whose completion is the socket-death signal).
    """

    counter = 0

    def __init__(self, *, fail_start=False):
        FakeWebsocket.counter += 1
        self.session_id = f"session-{FakeWebsocket.counter}"
        self.active_session = type("Session", (), {"id": self.session_id})()
        self._active_subscriptions = {}
        self._callbacks = {}
        self._tasks = [FakeTask(), FakeTask()]
        self._running = True
        self._closing = False
        self._socket_thread = None
        # The real ladder `_connect` walks, mirrored so a test can see it
        # emptied rather than merely set.
        self.reconnect_delay_steps = [0, 1, 2, 4, 8, 16, 32, 64, 128]
        self._next_id = 0
        self.started = False
        self.stopped = False
        self.fail_start = fail_start
        self.raise_on_subscribe = None
        # Feature 007. The two coverage types fail independently on the real
        # thing -- a channel can refuse `channel.chat.notification` while its
        # `channel.chat.message` subscription is live -- so the double has to
        # be able to model that. `raise_on_subscribe` stays the both-types
        # fallback the pre-007 tests set.
        self.raise_on_subscribe_by_type = {}
        # (subscription_type, broadcaster_user_id, user_id) per listen call, so
        # a test can prove that a repair created ONLY the missing type and did
        # not duplicate the surviving one (FR-002).
        self.listen_calls = []

    def start(self):
        if self.fail_start:
            raise RuntimeError("could not connect")
        self.started = True

    async def stop(self):
        self.stopped = True

    def _mint_id(self, subscription_type):
        """A fresh id, distinguishable by type.

        Separate id spaces per type are the point: nothing in the pool may
        infer one half of a channel's pair from the other's id.
        """
        self._next_id += 1
        suffix = "notice" if subscription_type == NOTIFICATION_TYPE else "sub"
        return f"{self.session_id}-{suffix}-{self._next_id}"

    def _listen(self, subscription_type, broadcaster_user_id, user_id, callback):
        failure = self.raise_on_subscribe_by_type.get(subscription_type)
        if failure is None:
            failure = self.raise_on_subscribe
        if failure is not None:
            raise failure
        self.listen_calls.append((subscription_type, broadcaster_user_id, user_id))
        subscription_id = self._mint_id(subscription_type)
        self._active_subscriptions[subscription_id] = {
            "sub_type": subscription_type,
            "condition": {"broadcaster_user_id": broadcaster_user_id, "user_id": user_id},
            "callback": callback,
        }
        self._callbacks[subscription_id] = {"callback": callback}
        return subscription_id

    async def listen_channel_chat_message(self, broadcaster_user_id, user_id, callback):
        return self._listen(CHAT_TYPE, broadcaster_user_id, user_id, callback)

    async def listen_channel_chat_notification(
        self, broadcaster_user_id, user_id, callback
    ):
        """The auxiliary half. Same shape as the chat listener in 4.5.0."""
        return self._listen(NOTIFICATION_TYPE, broadcaster_user_id, user_id, callback)

    def subscriptions_of_type(self, subscription_type):
        return {
            subscription_id
            for subscription_id, subscription in self._active_subscriptions.items()
            if subscription.get("sub_type") == subscription_type
        }

    def die(self):
        """What a socket that cannot reconnect looks like: the receive task ends."""
        self._tasks = [FakeTask(finished=True), FakeTask()]

    def reconnect(self, session_id=None):
        """A new session on the same connection, which is what a reconnect is.

        Everything Twitch held on the old session is gone with it, whatever
        the library's own registry still claims.
        """
        self.session_id = session_id or f"session-reconnected-{id(self)}"
        self.active_session = type("Session", (), {"id": self.session_id})()
        return self.session_id

    def rotate_ids(self):
        """What a keepalive-loss reconnect does: same channels, new ids.

        Each subscription keeps its own type, so a rotated pair still has one
        id per coverage type and neither can be resolved from the other.
        """
        rotated = {}
        for subscription in self._active_subscriptions.values():
            rotated[self._mint_id(subscription.get("sub_type"))] = subscription
        self._active_subscriptions = rotated
        self._callbacks = {key: {"callback": None} for key in rotated}


class FakePoolTwitch:
    """The Twitch client surface the pool uses: users, list, delete."""

    def __init__(self, subscriptions=None, user_id="99"):
        self.user_id = user_id
        self.subscriptions = list(subscriptions or [])
        self.deleted = []
        self.not_found = set()
        # Feature 007. Enumeration is two type-filtered Helix walks, so the
        # double has to filter by `sub_type`, record which walks ran, and be
        # able to fail exactly one of them (R1).
        self.listed_types = []
        self.list_errors = {}
        self.list_errors_after = {}
        # subscription id -> exception, for a one-sided delete failure.
        self.delete_errors = {}

    def get_users(self):
        async def pages():
            yield type("User", (), {"id": self.user_id})()

        return pages()

    async def get_eventsub_subscriptions(self, sub_type=None, target_token=None):
        self.listed_types.append(sub_type)
        error = self.list_errors.get(sub_type)
        if error is not None:
            raise error
        rows = [
            row
            for row in self.subscriptions
            if sub_type is None
            or getattr(row, "type", None) is None
            or getattr(row, "type", None) == sub_type
        ]
        fail_after = self.list_errors_after.get(sub_type)

        class Result:
            total = 10 ** 6  # Deliberately a lie. Nothing may read it.

            def __aiter__(self):
                async def gen():
                    for index, row in enumerate(rows):
                        if fail_after is not None and index >= fail_after:
                            raise eventsub_pool.TwitchAPIException(
                                f"helix walk for {sub_type} failed part way"
                            )
                        yield row

                return gen()

            def current_cursor(self):
                return None

        return Result()

    async def delete_eventsub_subscription(self, subscription_id, target_token=None):
        if subscription_id in self.not_found:
            raise eventsub_pool.TwitchResourceNotFound("subscription not found")
        error = self.delete_errors.get(subscription_id)
        if error is not None:
            raise error
        self.deleted.append(subscription_id)


def make_pool(cap=SUBSCRIPTIONS_PER_CONNECTION, twitch=None, handler=None, **kwargs):
    """A pool whose connections are fakes, so no socket is ever opened."""
    return EventSubPoolTransport(
        twitch or FakePoolTwitch(),
        handler or AsyncMock(),
        user_id="99",
        cap=cap,
        connection_factory=FakeWebsocket,
        **kwargs,
    )


def existing_subscription(
    subscription_id,
    broadcaster_id,
    session_id,
    status="enabled",
    subscription_type=CHAT_TYPE,
):
    """One row as Helix reports it, for either coverage type.

    `type` is what makes the two `list()` walks separable: a chat row must
    never satisfy the notification walk, or a channel with only half its pair
    would enumerate as complete.
    """
    return type(
        "Sub",
        (),
        {
            "id": subscription_id,
            "status": status,
            "type": subscription_type,
            "condition": {"broadcaster_user_id": str(broadcaster_id)},
            "transport": {"method": "websocket", "session_id": session_id},
        },
    )()


def notification_subscription(subscription_id, broadcaster_id, session_id, status="enabled"):
    """The auxiliary half of a pair, as Helix reports it."""
    return existing_subscription(
        subscription_id,
        broadcaster_id,
        session_id,
        status,
        subscription_type=NOTIFICATION_TYPE,
    )


# ---------------------------------------------------------------------------
# Feature 007 -- dual-coverage helpers (T001)
# ---------------------------------------------------------------------------


def make_dual_pool(
    cap=SUBSCRIPTIONS_PER_CONNECTION,
    twitch=None,
    handler=None,
    notification_handler=None,
    **kwargs,
):
    """A pool wired the way Feature 007 wires it: both coverage types.

    The notification handler is only a SINK for `channel.chat.notification`
    events. Dual coverage itself is not conditional on it -- every monitored
    channel gets both subscriptions (FR-001) whether or not anything is
    listening -- so passing it here is about being able to assert which
    handler an event reached, not about switching the second type on.
    """
    return EventSubPoolTransport(
        twitch or FakePoolTwitch(),
        handler or AsyncMock(),
        notification_handler=notification_handler or AsyncMock(),
        user_id="99",
        cap=cap,
        connection_factory=FakeWebsocket,
        **kwargs,
    )


class FakeMonotonicMs:
    """An injectable monotonic clock, in milliseconds.

    The auxiliary-refusal hold-off is an hour long. It cannot be tested
    against the real clock, and sleeping through it is not a test -- so the
    pool takes its monotonic reading from here instead.
    """

    def __init__(self, now_ms=1_000_000):
        self.now_ms = now_ms

    def __call__(self):
        return self.now_ms

    def advance_seconds(self, seconds):
        self.now_ms += int(seconds * 1000)
        return self.now_ms


def chat_slot(pool, broadcaster_id):
    """The channel's `channel.chat.message` slot, or None."""
    return pool._slots.get((broadcaster_id, eventsub_pool.CoverageType.CHAT))


def notification_slot(pool, broadcaster_id):
    """The channel's `channel.chat.notification` slot, or None."""
    return pool._slots.get((broadcaster_id, eventsub_pool.CoverageType.NOTIFICATION))


def coverage_state(pool, broadcaster_id):
    return pool.channel_coverage(broadcaster_id).state


def listen_calls_of(websocket, subscription_type):
    return [call for call in websocket.listen_calls if call[0] == subscription_type]


def refusal_error():
    """The 403 wording `_classify` already maps to a refusal."""
    return eventsub_pool.EventSubSubscriptionError(
        "subscription missing proper authorization"
    )


async def make_chat_only(pool, broadcaster_id, connection=None):
    """Leave a channel with live chat and no notification coverage.

    Twitch's own 500 on the auxiliary type, which is retryable and therefore
    starts no hold-off -- the plain `chat_only` state, not the degraded one.
    """
    connection = connection or (
        pool._connections[0] if pool._connections else await pool._grow()
    )
    websocket = connection.websocket
    websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = (
        eventsub_pool.TwitchBackendException("twitch 500")
    )
    try:
        with pytest.raises(TransportError):
            await pool.create(broadcaster_id)
    finally:
        websocket.raise_on_subscribe_by_type.pop(NOTIFICATION_TYPE, None)
    return connection


async def make_degraded(pool, broadcaster_id, connection=None):
    """Drive D2's exact case: chat live, Twitch refuses the notification."""
    connection = connection or (
        pool._connections[0] if pool._connections else await pool._grow()
    )
    websocket = connection.websocket
    websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = refusal_error()
    try:
        return await pool.create(broadcaster_id)
    finally:
        websocket.raise_on_subscribe_by_type.pop(NOTIFICATION_TYPE, None)


def make_notification_event(
    broadcaster_id=123,
    notice_type="raid",
    message_id="notice-uuid",
    occurred_at=None,
    viewer_count=None,
    total=None,
):
    """A stand-in for `ChannelChatNotificationEvent`, shaped like the real one.

    `TwitchObject.__init__` skips any field the payload omits, so only the
    sub-object that matches `notice_type` is present at all -- an absent one
    is a missing attribute, not a None. The envelope timestamp is already a
    tz-aware datetime by the time a callback runs, exactly as on the chat path.
    """
    data = {
        "broadcaster_user_id": str(broadcaster_id),
        "broadcaster_user_login": "a_streamer",
        "chatter_user_id": "456",
        "notice_type": notice_type,
        "message_id": message_id,
    }
    if notice_type == "raid":
        data["raid"] = type("Raid", (), {"viewer_count": viewer_count or 0})()
    elif notice_type == "sub_gift":
        data["sub_gift"] = type("SubGift", (), {"cumulative_total": total})()
    elif notice_type == "community_sub_gift":
        data["community_sub_gift"] = type("CommunitySubGift", (), {"total": total or 1})()

    return type(
        "NotificationEvent",
        (),
        {
            "metadata": type(
                "Meta",
                (),
                {
                    "subscription_type": NOTIFICATION_TYPE,
                    "message_timestamp": occurred_at
                    or datetime(2026, 9, 4, 12, 0, 0, 250000, tzinfo=timezone.utc),
                },
            )(),
            "event": type("NotificationData", (), data)(),
        },
    )()


class TestPoolRouting:
    """T019a / FR-006, D6 -- where a channel lands, and that it stays there."""

    def test_routing_is_stable_across_reconciles(self):
        """The same broadcaster comes back to the same connection.

        This is the whole point of D6. If routing moved, a socket death would
        force a reshuffle of the entire pool instead of costing only the
        subscriptions that were actually on the dead socket.

        Placement is compared per (channel, coverage type), because that is
        what a slot is now: a channel is stable only when BOTH of its
        subscriptions come back to where they were.

        The pool is given its two connections up front, so this measures
        routing and not the order the channels happened to arrive in -- see
        `test_a_growing_pool_does_not_move_what_it_already_placed` for that.
        The channel count stays well inside the 600 subscriptions two sessions
        hold, so capacity never binds and what is measured is routing alone.
        """

        async def run():
            pool = make_pool()
            await pool._grow()
            await pool._grow()

            async def fill(order):
                for broadcaster_id in order:
                    await pool.create(broadcaster_id)
                placed = {key: slot.connection_id for key, slot in pool._slots.items()}
                # One delete per CHANNEL: the chat id is the handle the
                # reconciler holds, and it takes both halves with it.
                for broadcaster_id in sorted({key[0] for key in pool._slots}):
                    await pool.delete(chat_slot(pool, broadcaster_id).subscription_id)
                assert pool._slots == {}
                return placed

            channels = range(1, 201)
            first = await fill(channels)
            second = await fill(reversed(channels))
            assert second == first
            assert len(first) == 2 * len(channels), "a channel lost half its pair"
            # And both connections are actually used, so "stable" is not just
            # "everything landed on connection 0".
            assert len(set(first.values())) == 2
            # With room on the connection the pair is kept together, so a
            # socket death costs whole channels rather than half of twice as
            # many (R2 allows a split; it must not happen with room to spare).
            for broadcaster_id in channels:
                assert (
                    first[(broadcaster_id, eventsub_pool.CoverageType.CHAT)]
                    == first[(broadcaster_id, eventsub_pool.CoverageType.NOTIFICATION)]
                )

        asyncio.run(run())

    def test_a_growing_pool_does_not_move_what_it_already_placed(self):
        """Growth places new channels; it never reshuffles the existing ones.

        A cold start therefore fills the first connection to the cap before
        the second one opens, and the pool stays lopsided until those channels
        are dropped for their own reasons. That is the deliberate trade: an
        even split would mean moving -- and so re-creating -- subscriptions
        that are working, every time the pool grows.

        The cap is in subscriptions, so the first session is full at 150
        channels, and every slot already placed is checked by its own
        (channel, coverage type) key.
        """

        async def run():
            pool = make_pool()
            channels_per_connection = SUBSCRIPTIONS_PER_CONNECTION // 2
            for broadcaster_id in range(1, channels_per_connection + 1):
                await pool.create(broadcaster_id)
            assert len(pool._connections) == 1
            assert pool.occupancy() == {"0": SUBSCRIPTIONS_PER_CONNECTION}
            before = {key: slot.connection_id for key, slot in pool._slots.items()}

            for broadcaster_id in range(channels_per_connection + 1, 251):
                await pool.create(broadcaster_id)

            assert len(pool._connections) == 2
            for key, connection_id in before.items():
                assert pool._slots[key].connection_id == connection_id

        asyncio.run(run())

    def test_routing_survives_process_restart(self):
        """Routing must not depend on PYTHONHASHSEED.

        The built-in `hash()` of a str is salted per process. Using it would
        make every restart reshuffle every channel across the pool, which is
        the failure D6 exists to prevent, and no test inside one process would
        ever catch it.
        """
        scores = [eventsub_pool._score(4242, index) for index in range(4)]
        assert scores == [eventsub_pool._score(4242, index) for index in range(4)]
        # A literal, so a change of hash function has to be deliberate.
        assert eventsub_pool._score(4242, 0) == int.from_bytes(
            __import__("hashlib").blake2b(b"4242:0", digest_size=8).digest(), "big"
        )

    def test_losing_a_connection_moves_only_its_own_channels(self):
        """Rendezvous hashing, not modulo.

        With `hash(id) % len(connections)`, removing one connection of three
        re-routes about two thirds of ALL channels. Here only the channels
        that were on the dead connection may move.
        """

        async def run():
            pool = make_pool()
            for _ in range(3):
                await pool._grow()
            ids = list(range(1, 151))  # well under the cap, so capacity never binds
            before = {bid: pool.route(bid).connection_id for bid in ids}

            doomed = pool._connections[1]
            pool._retire(doomed)
            after = {bid: pool.route(bid).connection_id for bid in ids}

            for broadcaster_id in ids:
                if before[broadcaster_id] != doomed.connection_id:
                    assert after[broadcaster_id] == before[broadcaster_id]

        asyncio.run(run())


class TestPoolOccupancy:
    """T019a / T019 -- the cap holds and the count is the pool's own."""

    def test_occupancy_never_exceeds_the_cap(self):
        """The cap is in SUBSCRIPTIONS, and a channel now costs two of them.

        The channel count is derived from that, not chosen: 350 channels are
        700 subscriptions, which the three sessions Twitch allows can hold.
        """

        async def run():
            pool = make_pool()
            channels = 350
            for broadcaster_id in range(1, channels + 1):
                await pool.create(broadcaster_id)
            counts = pool.occupancy()
            assert sum(counts.values()) == channels * 2
            assert all(count <= SUBSCRIPTIONS_PER_CONNECTION for count in counts.values())
            assert pool.coverage_counts().get("complete") == channels, (
                "channels were counted in subscriptions, or a pair went missing"
            )

        asyncio.run(run())

    def test_concurrent_creates_do_not_oversubscribe(self):
        """Workers routing at once must not overfill one session.

        Occupancy is only visible after a create returns, so without a
        reservation the last few creates of a filling connection all see room
        and all take it -- and each create now takes TWO subscriptions, so a
        reservation counted in channels would overfill it just as surely.
        """

        async def run():
            pool = make_pool(cap=10)
            await asyncio.gather(*(pool.create(bid) for bid in range(1, 13)))
            counts = pool.occupancy()
            assert sum(counts.values()) == 24
            assert all(count <= 10 for count in counts.values())
            assert all(connection.reserved == 0 for connection in pool._connections)
            assert all(coverage_state(pool, bid) == "complete" for bid in range(1, 13))

        asyncio.run(run())

    def test_pool_grows_at_the_cap_boundary(self):
        """One connection up to 300 SUBSCRIPTIONS, a second at 301 (T018).

        Which is 150 channels and then the 151st, because the boundary the
        pool grows at has never been a channel count.
        """

        async def run():
            pool = make_pool()
            channels_per_connection = SUBSCRIPTIONS_PER_CONNECTION // 2
            for broadcaster_id in range(1, channels_per_connection + 1):
                await pool.create(broadcaster_id)
            assert len(pool._connections) == 1
            assert pool.occupancy() == {"0": SUBSCRIPTIONS_PER_CONNECTION}

            await pool.create(channels_per_connection + 1)
            assert len(pool._connections) == 2
            assert sum(pool.occupancy().values()) == SUBSCRIPTIONS_PER_CONNECTION + 2
            # The whole pair moved on: a connection with room for one
            # subscription is not a connection with room for a channel.
            assert pool.occupancy() == {"0": SUBSCRIPTIONS_PER_CONNECTION, "1": 2}

        asyncio.run(run())

    def test_no_connection_is_opened_before_there_is_work(self):
        """Twitch closes a session that has no subscription within 10 s."""

        async def run():
            pool = make_pool()
            await pool.start()
            assert pool._connections == []
            assert pool.occupancy() == {}

        asyncio.run(run())

    def test_occupancy_is_not_read_back_from_the_library(self):
        """T019. The library's own count is measured-wrong; ours is authoritative."""

        async def run():
            pool = make_pool()
            for broadcaster_id in range(1, 6):
                await pool.create(broadcaster_id)
            # Corrupt the library's view the way the spike saw it corrupted.
            pool._connections[0]._active_subscriptions = {}
            pool._connections[0].websocket._active_subscriptions = {}
            # Five channels, ten subscriptions -- occupancy answers in the
            # second unit and takes it from its own bookkeeping.
            assert pool.occupancy() == {"0": 10}

        asyncio.run(run())


class TestPoolDeletes:
    """T024 -- a subscription that is already gone is not a failure."""

    def test_delete_of_a_lingering_subscription_succeeds(self):
        """`websocket_disconnected` leftovers answer "not found" on DELETE."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            subscription_id = await pool.create(7)
            twitch.not_found.add(
                next(iter(pool._connections[0].websocket._active_subscriptions))
            )
            await pool.delete(subscription_id)  # must not raise
            assert pool.occupancy() == {"0": 0}
            assert pool._slots == {}

        asyncio.run(run())

    def test_delete_follows_an_id_a_reconnect_rotated(self):
        """A reconnect re-subscribes everything and every id changes.

        Deleting the id recorded at create time would answer "not found" while
        the real subscription kept delivering into a channel nobody wants --
        and it is now one rotated id per coverage type, so each half has to be
        followed to its own new id.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            recorded = await pool.create(7)
            recorded_ids = {
                chat_slot(pool, 7).subscription_id,
                notification_slot(pool, 7).subscription_id,
            }
            websocket = pool._connections[0].websocket
            websocket.rotate_ids()
            live = set(websocket._active_subscriptions)
            assert len(live) == 2
            assert live.isdisjoint(recorded_ids)

            await pool.delete(recorded)
            assert set(twitch.deleted) == live
            assert len(twitch.deleted) == 2, "a half was deleted twice, or leaked"
            assert recorded not in twitch.deleted, (
                "the delete followed the recorded id, so the live subscription "
                "went on delivering"
            )
            # And the library must not resubscribe either half on its next
            # reconnect.
            assert websocket._active_subscriptions == {}
            assert websocket._callbacks == {}
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_delete_reports_a_real_failure(self):
        """Only "not found" is success. A 401 is still an error."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            subscription_id = await pool.create(7)

            async def boom(sub_id, target_token=None):
                raise eventsub_pool.TwitchAPIException("unauthorized")

            twitch.delete_eventsub_subscription = boom
            with pytest.raises(TransportError):
                await pool.delete(subscription_id)

        asyncio.run(run())


class TestPoolEnumeration:
    """FR-005 -- what `list()` counts, and what it refuses to count."""

    def test_list_counts_pages_and_ignores_total(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.create(1)
            await pool.create(2)
            session = pool._connections[0].websocket.session_id
            # Both types for both channels: a channel is only complete when
            # each of its two walks reports it.
            twitch.subscriptions = [
                existing_subscription("a", 1, session),
                notification_subscription("a-notice", 1, session),
                existing_subscription("b", 2, session),
                notification_subscription("b-notice", 2, session),
            ]
            seen = [sub async for sub in pool.list()]
            assert {sub.broadcaster_id for sub in seen} == {1, 2}
            # `total` on the result object claims a million, and four rows
            # joined into two channels.
            assert len(seen) == 2
            assert {sub.subscription_id for sub in seen} == {"a", "b"}, (
                "the auxiliary id was handed back as the channel's handle"
            )

        asyncio.run(run())

    def test_list_skips_subscriptions_on_a_session_the_pool_does_not_hold(self):
        """A dead session's subscriptions can never deliver to this process.

        Counting one in the actual set would make the reconciler believe a
        channel is covered while it is silently dark. The foreign channel is
        given BOTH halves, so what is rejected is the session and not merely
        an incomplete pair.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.create(1)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription("a", 1, session),
                notification_subscription("a-notice", 1, session),
                existing_subscription("b", 2, "a-session-from-a-dead-process"),
                notification_subscription("b-notice", 2, "a-session-from-a-dead-process"),
            ]
            seen = [sub async for sub in pool.list()]
            assert [sub.broadcaster_id for sub in seen] == [1]

        asyncio.run(run())

    def test_list_at_startup_yields_nothing(self):
        """No connections yet, so nothing on disk belongs to this process."""

        async def run():
            twitch = FakePoolTwitch(
                subscriptions=[existing_subscription("a", 1, "old-session")]
            )
            pool = make_pool(twitch=twitch)
            assert [sub async for sub in pool.list()] == []

        asyncio.run(run())


class TestPoolErrorClassification:
    """The reconciler acts on the exception type, so the mapping matters."""

    @pytest.mark.parametrize(
        "message,expected",
        [
            ("subscription missing proper authorization", SubscriptionRefusedError),
            ("Forbidden", SubscriptionRefusedError),
            ("Too Many Requests", RateLimitedError),
            ("you have exceeded the rate limit", RateLimitedError),
            (
                "websocket transport session does not exist or has already disconnected",
                TransientSessionError,
            ),
            ("the websocket session has already disconnected", TransientSessionError),
            ("something else entirely", TransportError),
        ],
    )
    def test_error_text_maps_to_the_right_exception(self, message, expected):
        """pyTwitchAPI throws away the HTTP status, so the text is all there is."""

        async def run():
            pool = make_pool()
            websocket_error = eventsub_pool.EventSubSubscriptionError(message)
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = websocket_error
            with pytest.raises(expected):
                await pool.create(5)

        asyncio.run(run())

    def test_a_refusal_is_not_mistaken_for_a_rate_limit(self):
        """Order matters: a refusal must never enter the 429 retry loop."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError(
                    "subscription missing proper authorization"
                )
            )
            with pytest.raises(SubscriptionRefusedError):
                await pool.create(5)

        asyncio.run(run())

    def test_a_full_session_is_not_a_rate_limit(self):
        """A full session answers 400. Retrying routes straight back to it."""

        async def run():
            pool = make_pool()
            await pool._grow()
            retired = pool._connections[0].websocket
            retired.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError(
                    "websocket session has too many subscriptions"
                )
            )
            with pytest.raises(TransportError) as caught:
                await pool.create(5)
            assert not isinstance(caught.value, RateLimitedError)
            # Full while holding nothing means the session can carry nothing.
            # Merely skipping it in routing left its thread, event loop and
            # ClientSession leaked for the life of the process, because
            # `_is_dead()` cannot flag a socket the library still likes.
            assert pool._connections == [], "the unusable connection was kept"
            assert retired._closing is True, "its socket was never torn down"

            # And the next create opens a fresh one.
            retired.raise_on_subscribe = None
            await pool.create(5)
            assert len(pool._connections) == 1

        asyncio.run(run())

    def test_a_connection_that_reported_full_returns_to_routing_when_it_drains(self):
        """`full` used to be a permanent flag, so one report retired a socket
        from routing for the life of the process. Under ordinary hysteresis
        churn the pool then opened fresh sockets while drained ones sat idle,
        growing the socket count without bound."""

        async def run():
            pool = make_pool()
            await pool._grow()
            connection = pool._connections[0]
            # Three channels land -- six subscriptions, which is the unit the
            # session is full in -- then Twitch calls the session full.
            for broadcaster_id in range(3):
                await pool.create(broadcaster_id)
            assert connection.occupancy == 6
            connection.websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError("subscription limit reached")
            )
            with pytest.raises(TransportError):
                await pool.create(99)
            assert connection.full_at == 6
            assert connection.reserved == 0, "the refused pair kept its reservation"
            assert pool.route(99, slots=2) is not connection

            # Dropping one channel frees the two subscriptions that takes it
            # back under the level it refused at.
            connection.websocket.raise_on_subscribe = None
            await pool.delete(chat_slot(pool, 0).subscription_id)
            assert connection.occupancy == 4
            assert pool.route(99, slots=2) is connection

        asyncio.run(run())

    def test_a_reconnect_race_is_not_mistaken_for_a_full_session(self):
        """`research.md` records the library raising
        `websocket session has already disconnected` twice from its own
        `_resubscribe` during a 500-channel ramp. A marker list wide enough to
        match that text classified a transient reconnect race as a full
        session -- and at occupancy 0 that retired a perfectly good socket."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError(
                    "websocket session has already disconnected"
                )
            )
            with pytest.raises(TransportError):
                await pool.create(5)

            assert len(pool._connections) == 1, "a live connection was retired"
            assert pool._connections[0].full_at is None

        asyncio.run(run())

    def test_an_unclassified_error_is_logged_with_its_raw_message(self, caplog):
        """The marker lists are string matches against wording nobody has seen
        from a full session. A miss must leave a diagnosable trace."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError("some entirely new wording")
            )
            with caplog.at_level(logging.WARNING, logger="stream_monitoring"):
                with pytest.raises(TransportError):
                    await pool.create(5)
            # The raw text rides in `extra`, which is what the JSON handler
            # ships and what makes the miss diagnosable.
            unclassified = [
                record for record in caplog.records
                if "Unclassified" in record.getMessage()
            ]
            assert len(unclassified) == 1
            assert unclassified[0].error == "some entirely new wording"

        asyncio.run(run())

    def test_a_reserved_slot_is_released_when_the_create_fails(self):
        """Otherwise a run of failures would slowly fill the pool with nothing."""

        async def run():
            pool = make_pool()
            await pool._grow()
            pool._connections[0].websocket.raise_on_subscribe = (
                eventsub_pool.EventSubSubscriptionError("Too Many Requests")
            )
            for broadcaster_id in range(20):
                with pytest.raises(RateLimitedError):
                    await pool.create(broadcaster_id)
            assert pool._connections[0].reserved == 0
            assert len(pool._connections) == 1

        asyncio.run(run())

    def test_the_reservation_is_held_until_the_subscription_is_recorded(self):
        """`load` must never dip between giving up the slot and recording the
        subscription.

        It used to: `_release` and the record block took the lock separately,
        so `reserved` fell to 0 while `subscription_ids` was still empty. A
        worker already queued on the lock at that moment -- which is the
        ordinary case at concurrency 10 -- is granted it before the record
        block runs, routes against the dip, and reserves a slot that is
        already spoken for. The session then goes one past its cap and Twitch
        refuses the overflow.

        The dip is what makes that possible, so the dip is what this asserts.
        Sampling at every lock release covers the whole create, including the
        window between the two acquisitions the old code left open.

        The cap holds a channel and a half now, so the second create stays on
        the same connection and the samples are of the connection under test.
        With a pair reserved, `load` sits at 4 from the reservation until both
        halves are recorded, and every release in between must see that.
        """

        async def run():
            pool = make_pool(cap=4)
            await pool._grow()
            connection = pool._connections[0]
            await pool.create(1)          # a pair recorded, so load starts at 2

            samples = []
            real_lock = pool._lock

            class ProbedLock:
                async def __aenter__(self):
                    await real_lock.acquire()

                async def __aexit__(self, *exc):
                    samples.append(connection.load)
                    real_lock.release()

            pool._lock = ProbedLock()
            await pool.create(2)

            assert samples, "the create took no lock at all"
            assert len(samples) >= 3, (
                f"the pair took {len(samples)} critical sections; the reserve "
                "and both records are the windows this samples"
            )
            assert min(samples) == 4, (
                f"load dipped to {min(samples)} mid-create (samples {samples}); "
                "a worker routing in that window would oversubscribe the session"
            )
            assert (connection.occupancy, connection.reserved) == (4, 0)

        asyncio.run(run())


class TestPoolSocketDeath:
    """T023 / R4 -- a dead socket's channels go back to "not subscribed"."""

    def test_a_dead_connection_is_dropped_and_reported(self):
        """The loss is reported in SUBSCRIPTIONS: five channels are ten."""

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            for broadcaster_id in range(1, 6):
                await pool.create(broadcaster_id)
            assert pool.occupancy() == {"0": 10}
            pool._connections[0].websocket.die()

            assert pool.reap_dead_connections() == 10
            assert lost == [10]
            assert pool._connections == []
            assert pool.occupancy() == {}
            assert pool._slots == {}
            assert all(
                coverage_state(pool, broadcaster_id) == "absent"
                for broadcaster_id in range(1, 6)
            )

        asyncio.run(run())

    def test_a_healthy_connection_is_left_alone(self):
        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.create(1)
            assert pool.reap_dead_connections() == 0
            assert lost == []
            assert len(pool._connections) == 1

        asyncio.run(run())

    def test_a_revoked_subscription_is_forgotten_and_reported(self):
        """A revocation stops delivery while every count still says "covered".

        Twitch revokes when the broadcaster withdraws authorization or the
        channel goes away. Without this the channel is silently dark and the
        subscription gauge never moves.

        Twitch revokes ONE subscription, so the payload carries its type and
        the sibling half must survive: what the channel loses is coverage of
        that type, not its place in the pool.
        """

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            subscription_id = await pool.create(7)
            notification_id = notification_slot(pool, 7).subscription_id

            await pool._on_revocation({
                "subscription": {
                    "id": subscription_id,
                    "type": CHAT_TYPE,
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "7"},
                }
            })
            await asyncio.sleep(0)  # let the threadsafe hop run

            assert lost == [1], "the pair was reported lost, not the subscription"
            assert chat_slot(pool, 7) is None
            assert notification_slot(pool, 7).subscription_id == notification_id
            assert coverage_state(pool, 7) == "notification_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_a_revocation_on_a_rotated_id_still_finds_the_channel(self):
        """A reconnect re-creates every subscription on the socket with new
        ids, and the pool keeps the ones it recorded at create time. So the
        revocation Twitch sends afterwards names an id `_by_subscription` has
        never seen, and the channel must still be found.

        The resolution has to come from the payload. `_handle_revocation` pops
        the id out of `_active_subscriptions` and `_callbacks` BEFORE it calls
        this handler, so any lookup in the library's registries is guaranteed
        to miss -- this test models that by emptying the registry, which is
        the state the handler really runs in. An earlier version of this test
        re-inserted the rotated id by hand and so proved nothing.

        Both the channel AND the type come out of the payload: a rotated pair
        has one new id per type, so the channel alone would not say which half
        Twitch withdrew.
        """

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            notification_id = notification_slot(pool, 7).subscription_id

            # The reconnect rotated the ids; the library has already forgotten
            # them by the time the revocation reaches us.
            websocket._active_subscriptions.clear()
            websocket._callbacks.clear()

            await pool._on_revocation({
                "subscription": {
                    "id": "rotated-sub-1",
                    "type": CHAT_TYPE,
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "7"},
                }
            })
            await asyncio.sleep(0)

            assert lost == [1], "the revocation was dropped, leaving the channel dark"
            assert chat_slot(pool, 7) is None
            assert notification_slot(pool, 7).subscription_id == notification_id, (
                "the rotated id resolved to the wrong half of the pair"
            )
            assert coverage_state(pool, 7) == "notification_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_an_unresolvable_revocation_still_reports_the_loss(self):
        """Twitch only delivers revocations for subscriptions on this pool's
        own sessions, so a revocation carrying no usable condition is still a
        channel this pool has lost track of. Re-enumerating costs one listing;
        staying quiet costs a permanently dark channel."""

        async def run():
            lost = []
            pool = make_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            await pool._on_revocation({"subscription": {"id": "not-ours"}})
            await asyncio.sleep(0)
            assert lost == [1]

        asyncio.run(run())

    def test_a_slot_from_a_previous_session_is_not_trusted(self):
        """The library's `_resubscribe()` restores the PRE-reconnect map
        wholesale when the FIRST re-subscribe fails (`if not
        self._active_subscriptions`). The registry then reports every old id as
        live while Twitch holds none of them on the new session -- so a
        registry check alone says yes for channels that do not exist, and the
        periodic re-adopt cannot repair it because `create()` keeps handing the
        ghost back. The session the slot was made on is the check that holds."""

        async def run():
            pool = make_pool()
            await pool.start()
            first_id = await pool.create(7)
            connection = pool._connections[0]

            # The reconnect: new session, and _resubscribe restored the old map
            # verbatim, so the registry still claims the old id.
            connection.websocket.active_session = type(
                "Session", (), {"id": "session-after-reconnect"}
            )()

            second_id = await pool.create(7)

            assert second_id != first_id, (
                "create() trusted a registry that survived a reconnect, so the "
                "channel is dark while every count says it is covered"
            )

        asyncio.run(run())

    def test_a_ghost_subscription_is_recreated_rather_than_handed_back(self):
        """`create()` short-circuits on the id it recorded, and a live
        connection is not proof of a live subscription: when the library's
        `_resubscribe()` gives up part way through a reconnect the socket stays
        up while the channels past the failure point no longer exist.

        Handing the recorded id back made no Twitch call, so the periodic
        re-adoption would drop the channel, ask for it again, be given the
        ghost straight back, and count it as covered for ever -- defeating the
        re-adoption that exists to catch exactly this. Each coverage type is
        judged on its own registry entry, so a ghost of either half has to be
        re-created rather than handed back.
        """

        async def run():
            pool = make_pool()
            await pool.start()
            first_id = await pool.create(7)
            first_notification_id = notification_slot(pool, 7).subscription_id
            websocket = pool._connections[0].websocket

            # The reconnect dropped this channel and never restored it.
            websocket._active_subscriptions.clear()
            websocket._callbacks.clear()

            second_id = await pool.create(7)

            assert second_id != first_id, (
                "create() handed back the id of a subscription that no longer "
                "exists, without contacting Twitch"
            )
            assert notification_slot(pool, 7).subscription_id != first_notification_id, (
                "the auxiliary ghost was handed back, so the channel is "
                "counted as covered while nothing delivers its notices"
            )
            assert coverage_state(pool, 7) == "complete"
            assert pool.occupancy() == {"0": 2}

        asyncio.run(run())

    def test_the_reconciler_recreates_what_the_dead_socket_held(self):
        """End to end: the loss reaches the reconciler and the next pass heals.

        The pool reports; the reconciler drives. The subscription count drops
        first -- that dip is the FR-012 alert -- and then recovers. The
        reconciler stays channel-keyed, so its own count is five channels
        while the pool holds their ten subscriptions.
        """

        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis)
            pool.on_subscriptions_lost = lambda lost: reconciler.invalidate_actual_set(lost)

            logins = [(f"c{i}", i) for i in range(1, 6)]
            seed_desired(fake_redis, logins)
            await reconciler.reconcile_once()
            assert reconciler.subscription_count == 5
            assert sum(pool.occupancy().values()) == 10
            assert pool.coverage_counts().get("complete") == 5
            first_connection = pool._connections[0]

            # The socket dies. Everything it held is gone -- both halves of
            # every channel that was on it.
            first_connection.websocket.die()
            pool.reap_dead_connections()
            assert pool.occupancy() == {}
            assert pool.coverage_counts() == {
                state: 0 for state in eventsub_pool.COVERAGE_STATES
            }
            assert reconciler_module.eventsub_subscription_count._value.get() == 0, (
                "the dip this alert is built on never reached the gauge"
            )

            # Twitch now reports nothing on a session we hold.
            twitch.subscriptions = []
            await reconciler.reconcile_once()

            assert reconciler.subscription_count == 5
            assert reconciler_module.eventsub_subscription_count._value.get() == 10
            assert pool._connections[0].connection_id != first_connection.connection_id
            assert sum(pool.occupancy().values()) == 10
            assert pool.coverage_counts().get("complete") == 5

        asyncio.run(run())


class TestPoolRaces:
    """Failure modes that only appear because the supervisor shares the loop."""

    def test_a_connection_retired_mid_create_leaves_no_ghost_slot(self):
        """The supervisor runs on this loop, and a create is an await.

        If a slot were recorded against a connection that has already been
        retired, nothing would ever clear it: `_retire` has been and gone, no
        revocation arrives for a dead session, and the reconciler does not
        delete a channel it still wants. Every later create would hand back
        that dead id without contacting Twitch, and the channel would be
        permanently dark while every count said "covered".
        """

        async def run():
            pool = make_pool()
            await pool._grow()
            connection = pool._connections[0]
            original = connection.websocket.listen_channel_chat_message

            async def retire_then_subscribe(*args, **kwargs):
                pool._retire(connection)
                return await original(*args, **kwargs)

            connection.websocket.listen_channel_chat_message = retire_then_subscribe

            with pytest.raises(TransportError):
                await pool.create(7)
            assert pool._slots == {}
            assert pool._by_subscription == {}

        asyncio.run(run())

    def test_a_slot_on_a_retired_connection_is_not_handed_back(self):
        """The early return must check the connection, not just the slot.

        And it must do so per coverage type: a stale chat slot and a stale
        notification slot are two independent records, and either one handed
        back would leave the channel counted as covered while nothing
        delivers for it.
        """

        async def run():
            pool = make_pool()
            subscription_id = await pool.create(7)
            stale_notification_id = notification_slot(pool, 7).subscription_id
            # Retire without going through _retire, the way a stale slot could
            # survive a bookkeeping slip.
            pool._connections = []

            recreated = await pool.create(7)
            assert recreated != subscription_id
            assert notification_slot(pool, 7).subscription_id != stale_notification_id
            live_connection_id = pool._connections[0].connection_id
            assert chat_slot(pool, 7).connection_id == live_connection_id
            assert notification_slot(pool, 7).connection_id == live_connection_id
            assert coverage_state(pool, 7) == "complete"
            assert pool.occupancy() == {str(live_connection_id): 2}

        asyncio.run(run())

    def test_deleting_a_rotated_id_still_clears_the_library(self):
        """`list()` reports the id a reconnect made; `_by_subscription` has the old.

        Deleting the reported id and stopping there would leave the library's
        own registry intact, so the socket re-creates the channel on its next
        reconnect -- exactly the resurrection this cleanup exists to prevent.

        The reconciler drops a CHANNEL with the chat handle returned by
        `list()`, so that one call must also remove the sibling notification
        subscription.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.create(7)
            websocket = pool._connections[0].websocket
            websocket.rotate_ids()
            rotated_chat = next(iter(websocket.subscriptions_of_type(CHAT_TYPE)))
            rotated_notification = next(
                iter(websocket.subscriptions_of_type(NOTIFICATION_TYPE))
            )

            # The reconciler asks for the id Twitch reports, which the pool
            # has never seen.
            await pool.delete(rotated_chat)

            assert set(twitch.deleted) == {rotated_chat, rotated_notification}
            assert rotated_chat not in websocket._active_subscriptions
            assert rotated_chat not in websocket._callbacks
            assert chat_slot(pool, 7) is None
            assert websocket._active_subscriptions == {}
            assert websocket._callbacks == {}
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_a_cancelled_connect_tears_its_socket_down(self):
        """The same abandoned socket, reached the other way.

        Shutdown cancels the reconciler task, and that cancellation lands
        wherever the pass happens to be -- including inside `_grow`'s connect.
        `except Exception` does not catch `CancelledError`, so this path skipped
        the teardown the timeout path performs, and cancelling the future does
        not stop the executor thread already running `start()`. The socket is
        never appended to `_connections`, so `aclose()` cannot reach it either:
        a SIGTERM during a cold start leaked a spinning thread, its event loop
        and an open ClientSession, and kept the process from exiting cleanly.
        """

        class NeverConnects(FakeWebsocket):
            def start(self):
                self._startup_complete = False
                while not self._startup_complete:
                    time.sleep(0.01)

        async def run():
            pool = make_pool(connect_timeout_seconds=30)
            opened = []

            def factory():
                websocket = NeverConnects()
                opened.append(websocket)
                return websocket

            pool._connection_factory = factory

            creating = asyncio.create_task(pool.create(7))
            await asyncio.sleep(0.1)
            creating.cancel()
            with pytest.raises(asyncio.CancelledError):
                await creating

            assert opened, "no socket was built"
            assert opened[0]._startup_complete is True, (
                "the executor thread was left busy-waiting in start()"
            )
            assert opened[0]._closing is True, (
                "the abandoned socket's keep-alive loop was left spinning"
            )
            assert opened[0].reconnect_delay_steps == [], (
                "`_connect` ignores `_closing`, so a socket abandoned during a "
                "failing connect keeps a non-daemon thread through the whole "
                "255 s retry ladder and SIGTERM hangs joining it"
            )
            assert pool._connections == [], "a half-open socket joined the pool"

        asyncio.run(run())

    def test_a_reconnect_mid_create_is_not_recorded_as_current(self):
        """The library builds the POST's transport from the session that is
        current when the request goes out, and its socket thread can finish a
        reconnect while that request is in flight. Stamping the slot with the
        session read AFTER the await labelled a subscription made on the OLD
        session with the NEW one -- and then agreed with itself for ever: the
        session check passes, the registry holds the id because `_subscribe`
        added it, and `create()` hands that ghost back with no Twitch call
        while nothing delivers for the channel. `_resubscribe()` cannot save
        it either; it only re-creates what was in the registry when it took
        its snapshot.
        """

        async def run():
            pool = make_pool()
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            with pytest.raises(TransportError):
                await pool.create(7)

            assert pool._slots == {}, (
                "a subscription made on a closed session was recorded as current"
            )
            assert connection.occupancy == 0
            assert connection.reserved == 0, (
                "the reservation outlived the create -- both the chat slot's "
                "and the notification slot's, which was never attempted"
            )
            assert websocket._active_subscriptions == {}, (
                "the library would resurrect the dead id on its next reconnect"
            )

            # And the channel is simply retried, on the live session. Each
            # half carries its own honest stamp, read before its own listen.
            websocket.listen_channel_chat_message = original_listen
            assert await pool.create(7)
            assert chat_slot(pool, 7).session_id == "session-after-reconnect"
            assert notification_slot(pool, 7).session_id == "session-after-reconnect"
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_a_reconnect_mid_create_deletes_rather_than_guessing(self):
        """Which session the subscription landed on cannot be known out here.

        `_subscribe` reads the session when it builds the POST body, so a
        reconnect that finished BEFORE that moment puts the subscription on the
        NEW session -- live, with a callback -- and one that finished after
        puts it on the old one. Both look identical from the pool: the session
        changed across the await.

        Guessing "it is dead" and only dropping the library's callback was
        wrong for the live case: the subscription went on existing while the
        library had no callback for it, so it delivered into nothing, and the
        next pass took Twitch's 409 and adopted it -- `_adopt_conflict`
        restores the pool's indexes but not the callback -- leaving the channel
        counted and dark. Deleting is correct either way: the dead one answers
        "not found", the live one is removed and re-created cleanly.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            with pytest.raises(TransportError):
                await pool.create(7)

            assert twitch.deleted, (
                "the subscription was abandoned without being deleted, so a live "
                "one would linger with no callback and be adopted as covered"
            )
            assert pool._slots == {}
            assert websocket._active_subscriptions == {}
            assert connection.reserved == 0, "the reservation outlived the create"

        asyncio.run(run())

    def test_a_failed_delete_leaves_the_library_registry_alone(self):
        """The safe side of that error. If the DELETE fails we cannot know the
        subscription is gone, so the callback must stay: on the live-session
        branch it and the subscription are both still intact, and the next
        enumeration simply adopts something that works. Clearing it first
        would have thrown that away on a transient API error."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(twitch=twitch)
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            async def failing_delete(subscription_id, target_token=None):
                raise eventsub_pool.TwitchAPIException("twitch 503")

            twitch.delete_eventsub_subscription = failing_delete

            with pytest.raises(TransportError):
                await pool.create(7)

            assert websocket._active_subscriptions, (
                "the callback was dropped for a subscription that may still be live"
            )
            assert pool._slots == {}
            assert connection.reserved == 0

        asyncio.run(run())

    def test_the_reservation_is_held_across_the_discarding_delete(self):
        """The subscription may still exist on Twitch for the length of that
        round trip. Releasing the slot first counted it in neither `reserved`
        nor `subscription_ids`, so another worker could route a channel into a
        slot that was not really free and push the session past its cap.

        The reservation is pair-sized -- a new channel is two subscriptions --
        and the WHOLE of it has to survive the delete. The cap is one more
        than a pair, so a session that gave up even one of the two reads as
        having room for a channel it cannot take.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_pool(cap=3, twitch=twitch)
            await pool.start()
            await pool._grow()
            connection = pool._connections[0]
            websocket = connection.websocket
            original_listen = websocket.listen_channel_chat_message

            async def reconnect_during_create(broadcaster_user_id, user_id, callback):
                subscription_id = await original_listen(
                    broadcaster_user_id, user_id, callback
                )
                websocket.active_session = type(
                    "Session", (), {"id": "session-after-reconnect"}
                )()
                return subscription_id

            websocket.listen_channel_chat_message = reconnect_during_create

            during_delete = []

            async def slow_delete(subscription_id, target_token=None):
                during_delete.append((pool.route(7, slots=2), connection.reserved))
                await asyncio.sleep(0)

            twitch.delete_eventsub_subscription = slow_delete

            with pytest.raises(TransportError):
                await pool.create(7)

            assert during_delete == [(None, 2)], (
                f"saw {during_delete}: the slot looked free while the "
                "subscription might still exist, so another worker could "
                "oversubscribe the session"
            )
            assert connection.reserved == 0, "the reservation was never released"

        asyncio.run(run())

    def test_the_retry_ladder_is_emptied_only_after_the_socket_is_closed(self):
        """Ordering, and it is load-bearing.

        The moment `reconnect_delay_steps` is empty, `_connect` can unwind
        `run_until_complete` and STOP the socket loop -- and the teardown that
        closes the ClientSession is scheduled on that same loop. Emptying the
        list first therefore raced the cleanup it was paired with: the loop
        stopped with the teardown still pending, so the session was never
        closed and the connector leaked, with only a "coroutine was never
        awaited" warning to show for it.
        """

        class OrderRecordingSocket:
            """Records the two events whose ORDER is the whole point."""

            def __init__(self, order, loop):
                self.order = order
                self._socket_loop = loop
                self._connection = None
                self._session = self
                self._closing = False
                self._startup_complete = False
                self._running = True
                self._steps = [0, 1, 2, 4, 8]

            async def close(self):
                self.order.append("session closed")

            @property
            def reconnect_delay_steps(self):
                return self._steps

            @reconnect_delay_steps.setter
            def reconnect_delay_steps(self, value):
                if value == []:
                    self.order.append("ladder emptied")
                self._steps = value

        async def run():
            pool = make_pool()
            order = []
            websocket = OrderRecordingSocket(order, asyncio.get_running_loop())

            pool._abandon_socket(websocket)
            await asyncio.sleep(0.05)

            assert order == ["session closed", "ladder emptied"], (
                f"the ladder was emptied out of order (saw {order}); "
                "_connect can stop the socket loop the moment it is empty, "
                "leaving the teardown pending and the session unclosed"
            )
            assert websocket._closing is True
            assert websocket._startup_complete is True

        asyncio.run(run())

    def test_the_pool_refuses_to_grow_past_the_twitch_connection_limit(self):
        """Twitch allows 3 websocket connections with enabled subscriptions per
        client-id/user-id pair, so this transport tops out at 900 channels.
        Growth was unbounded and knew nothing about it. A fourth socket does
        not fail at connect time -- it fails later, per subscription, with an
        error this module cannot classify, and rendezvous routing keeps sending
        the same channels back to it: a silent retry loop with only a WARNING.
        """

        async def run():
            pool = make_pool(cap=1)
            await pool.start()
            for broadcaster_id in range(1, 4):
                await pool.create(broadcaster_id)
            assert len(pool._connections) == 3

            with pytest.raises(TransportError) as caught:
                await pool.create(4)
            assert "connection limit" in str(caught.value)
            assert len(pool._connections) == 3, "a fourth socket was opened"

        asyncio.run(run())

    def test_a_timed_out_connect_tears_its_socket_down(self):
        """`_keep_loop_alive()` spins on `while not self._closing`, and only
        `_stop()` sets that flag. A timed-out connect that only released the
        startup busy-wait left the socket thread spinning at 10 Hz for the life
        of the process, holding an open ClientSession -- and invisibly, since
        the connection is never appended to `_connections` and so
        `reap_dead_connections()` could never see it."""

        class NeverConnects(FakeWebsocket):
            def start(self):
                self._startup_complete = False
                while not self._startup_complete:
                    time.sleep(0.01)

        async def run():
            pool = make_pool(connect_timeout_seconds=0.3)
            pool._connection_factory = NeverConnects
            opened = []
            original = pool._connection_factory

            def factory():
                websocket = original()
                opened.append(websocket)
                return websocket

            pool._connection_factory = factory

            with pytest.raises(TransportError):
                await pool.create(7)

            assert opened, "no socket was built"
            assert opened[0]._closing is True, (
                "the abandoned socket's keep-alive loop was left spinning"
            )

        asyncio.run(run())

    def test_a_connection_that_never_comes_up_does_not_wedge_the_pool(self):
        """`start()` busy-waits on a flag only session_welcome sets.

        A socket thread that dies on the way up never sets it, so `start()`
        spins for the life of the process. Holding the growth lock across an
        unbounded wait would freeze every later create, and with it the
        reconciler.
        """

        class NeverConnects(FakeWebsocket):
            def start(self):
                self._startup_complete = False
                while not self._startup_complete:
                    time.sleep(0.01)

        async def run():
            pool = make_pool(connect_timeout_seconds=0.3)
            pool._connection_factory = NeverConnects

            with pytest.raises(TransportError):
                await pool.create(7)

            # A failed connect blocks growth briefly, so the rest of the batch
            # fails fast instead of each channel waiting out its own timeout.
            pool._connection_factory = FakeWebsocket
            with pytest.raises(TransportError):
                await pool.create(8)

            # The lock is free, so the pool still works once that window ends.
            pool._growth_blocked_until = 0.0
            assert await pool.create(7)
            assert pool.occupancy() == {"0": 2}
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())


# ---------------------------------------------------------------------------
# Feature 007 -- two coverage types per channel
# ---------------------------------------------------------------------------


class TestCoverageTypeContract:
    """T006 -- the closed set of coverage types, and what each maps to."""

    def test_there_are_exactly_two_coverage_types(self):
        """A third type would change every capacity number in the plan."""
        assert {member.value for member in eventsub_pool.CoverageType} == {
            "chat",
            "notification",
        }

    def test_each_type_names_its_twitch_subscription_type(self):
        assert eventsub_pool.CHAT_MESSAGE_SUBSCRIPTION_TYPE == CHAT_TYPE
        assert eventsub_pool.CHAT_NOTIFICATION_SUBSCRIPTION_TYPE == NOTIFICATION_TYPE
        assert eventsub_pool.CoverageType.CHAT.subscription_type == CHAT_TYPE
        assert (
            eventsub_pool.CoverageType.NOTIFICATION.subscription_type
            == NOTIFICATION_TYPE
        )


class TestPoolCoverageModel:
    """T006 / FR-001, FR-002 -- one slot per (broadcaster, coverage type)."""

    def test_a_channel_gets_one_slot_of_each_type(self):
        async def run():
            pool = make_dual_pool()
            await pool.create(7)

            assert chat_slot(pool, 7) is not None
            assert notification_slot(pool, 7) is not None
            assert (
                chat_slot(pool, 7).subscription_id
                != notification_slot(pool, 7).subscription_id
            ), "the two halves shared an id, so neither can be resolved alone"
            assert chat_slot(pool, 7).coverage_type is eventsub_pool.CoverageType.CHAT
            assert (
                notification_slot(pool, 7).coverage_type
                is eventsub_pool.CoverageType.NOTIFICATION
            )
            # Occupancy is a SUBSCRIPTION count, so one channel is two.
            assert pool.occupancy() == {"0": 2}

        asyncio.run(run())

    def test_create_returns_the_channel_s_chat_subscription_id(self):
        """The reconciler keys `_actual` by channel and hands this id back to
        `delete()`. The chat half is the channel's handle; the auxiliary half
        is resolved from the channel, never from the id."""

        async def run():
            pool = make_dual_pool()
            returned = await pool.create(7)
            assert returned == chat_slot(pool, 7).subscription_id

        asyncio.run(run())

    def test_the_subscription_index_resolves_each_id_to_its_own_slot(self):
        async def run():
            pool = make_dual_pool()
            await pool.create(7)
            chat = chat_slot(pool, 7)
            notification = notification_slot(pool, 7)

            assert pool._by_subscription[chat.subscription_id] is chat
            assert pool._by_subscription[notification.subscription_id] is notification

        asyncio.run(run())

    def test_an_unknown_channel_is_absent(self):
        async def run():
            pool = make_dual_pool()
            assert coverage_state(pool, 7) == "absent"
            assert pool.channel_coverage(7).chat_slot is None
            assert pool.channel_coverage(7).notification_slot is None

        asyncio.run(run())

    def test_both_slots_present_is_complete(self):
        async def run():
            pool = make_dual_pool()
            await pool.create(7)
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_chat_without_notification_is_chat_only(self):
        """Not `complete`, so the reconciler re-creates the missing half.

        With no hold-off in force this is the ordinary, repairable partial
        state -- the channel is NOT in the actual set.
        """

        async def run():
            pool = make_dual_pool()
            await pool._grow()
            await make_chat_only(pool, 7)

            assert chat_slot(pool, 7) is not None
            assert notification_slot(pool, 7) is None
            assert coverage_state(pool, 7) == "chat_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_notification_without_chat_is_notification_only(self):
        """The channel produces no chat at all, so it is equally incomplete."""

        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool.create(7)
            chat_id = chat_slot(pool, 7).subscription_id

            await pool._on_revocation({
                "subscription": {
                    "id": chat_id,
                    "type": CHAT_TYPE,
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "7"},
                }
            })
            await asyncio.sleep(0)

            assert chat_slot(pool, 7) is None
            assert notification_slot(pool, 7) is not None
            assert coverage_state(pool, 7) == "notification_only"

        asyncio.run(run())

    def test_a_refused_notification_with_live_chat_is_degraded_chat_only(self):
        """The one state where a channel is reported actual without both
        halves, and it exists only to stop an auxiliary refusal from evicting
        the channel's chat (D2)."""

        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)

            assert coverage_state(pool, 7) == "degraded_chat_only"
            assert pool.channel_coverage(7).auxiliary_refused_until_ms == (
                clock.now_ms + eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS * 1000
            )

        asyncio.run(run())

    def test_the_two_indexes_are_independent(self):
        """Dropping one type must leave the other's slot and id untouched."""

        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool.create(7)
            await pool.create(8)
            notification_id = notification_slot(pool, 7).subscription_id
            survivor = chat_slot(pool, 7).subscription_id

            await pool._on_revocation({
                "subscription": {
                    "id": notification_id,
                    "type": NOTIFICATION_TYPE,
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "7"},
                }
            })
            await asyncio.sleep(0)

            assert notification_id not in pool._by_subscription
            assert pool._by_subscription[survivor].broadcaster_id == 7
            assert coverage_state(pool, 7) == "chat_only"
            assert coverage_state(pool, 8) == "complete", "an unrelated channel moved"

        asyncio.run(run())


class TestPoolPairRouting:
    """T008 / R2 -- placement and reservation in subscription units."""

    def test_a_new_channel_needs_room_for_two_subscriptions(self):
        async def run():
            pool = make_dual_pool(cap=3)
            connection = await pool._grow()
            assert pool.route(9, slots=2) is connection

            await pool.create(8)  # two subscriptions, so one slot is left
            assert connection.load == 2
            assert pool.route(9, slots=2) is None, (
                "a pair was routed onto a connection with room for one"
            )
            assert pool.route(9, slots=1) is connection

        asyncio.run(run())

    def test_a_reservation_holds_the_number_of_subscriptions_it_will_create(self):
        async def run():
            pool = make_dual_pool(cap=10)
            connection = await pool._grow()

            reserved = await pool._reserve(9, slots=2)
            assert reserved is connection
            assert connection.reserved == 2
            assert connection.load == 2, "the cap was compared against one slot"

            await pool._release(connection, slots=2)
            assert connection.reserved == 0

        asyncio.run(run())

    def test_a_repair_prefers_the_connection_that_already_holds_the_channel(self):
        """Locality: keep a channel's pair together when there is room, even
        when rendezvous order would send the second half elsewhere."""

        async def run():
            pool = make_dual_pool(cap=10)
            first = await pool._grow()
            target = next(
                bid
                for bid in range(1, 5000)
                if eventsub_pool._score(bid, 1) > eventsub_pool._score(bid, 0)
            )
            await make_chat_only(pool, target, connection=first)
            second = await pool._grow()

            assert eventsub_pool._score(target, 1) > eventsub_pool._score(target, 0), (
                "the fixture no longer sets up the case it is testing"
            )
            assert pool.route(target, slots=1) is first, (
                "the repair was routed away from the connection holding the pair"
            )
            # And a channel with no slots still routes by rendezvous.
            untouched = next(
                bid
                for bid in range(5000, 10000)
                if eventsub_pool._score(bid, 1) > eventsub_pool._score(bid, 0)
            )
            assert pool.route(untouched, slots=2) is second

        asyncio.run(run())

    def test_a_pair_may_split_across_two_connections(self):
        """Legal and modelled (R2). A socket death then leaves the channel in
        a partial state, which is already convergent."""

        async def run():
            pool = make_dual_pool(cap=3)
            first = await pool._grow()
            await make_chat_only(pool, 7, connection=first)
            await pool.create(8)  # fills the connection: 1 + 2 == cap
            assert first.load == 3

            await pool.create(7)  # the repair has nowhere local to go

            assert chat_slot(pool, 7).connection_id == first.connection_id
            assert notification_slot(pool, 7).connection_id != first.connection_id
            assert coverage_state(pool, 7) == "complete"
            assert pool.occupancy() == {"0": 3, "1": 1}
            assert all(c.reserved == 0 for c in pool._connections)

        asyncio.run(run())

    def test_load_is_counted_in_subscriptions_not_channels(self):
        async def run():
            pool = make_dual_pool(cap=6)
            connection = await pool._grow()
            for broadcaster_id in (1, 2, 3):
                await pool.create(broadcaster_id)

            assert connection.occupancy == 6
            assert connection.load == 6
            assert pool.route(4, slots=2) is None, (
                "the cap was compared in channels, so the session went to 8 "
                "subscriptions against a 6-subscription limit"
            )

        asyncio.run(run())

    def test_concurrent_pair_creates_do_not_oversubscribe(self):
        """Ten workers reserve at once, and each now takes TWO slots.

        Reserving one and creating two is the way this overfills a session
        without any single create ever looking wrong.
        """

        async def run():
            pool = make_dual_pool(cap=10, max_connections=6)
            await asyncio.gather(*(pool.create(bid) for bid in range(1, 26)))

            counts = pool.occupancy()
            assert sum(counts.values()) == 50
            assert all(count <= 10 for count in counts.values())
            assert all(connection.reserved == 0 for connection in pool._connections)
            assert all(coverage_state(pool, bid) == "complete" for bid in range(1, 26))

        asyncio.run(run())

    def test_the_three_connection_limit_binds_in_subscription_units(self):
        async def run():
            pool = make_dual_pool(cap=2)
            await pool.start()
            for broadcaster_id in (1, 2, 3):
                await pool.create(broadcaster_id)
            assert len(pool._connections) == 3
            assert sum(pool.occupancy().values()) == 6

            with pytest.raises(TransportError) as caught:
                await pool.create(4)
            assert "connection limit" in str(caught.value)
            assert len(pool._connections) == 3

        asyncio.run(run())


class TestPoolPairCreation:
    """T010 / FR-002 -- create the missing types, and only those."""

    def test_an_absent_channel_creates_exactly_one_of_each_type(self):
        async def run():
            pool = make_dual_pool()
            await pool.create(7)
            websocket = pool._connections[0].websocket

            assert listen_calls_of(websocket, CHAT_TYPE) == [(CHAT_TYPE, "7", "99")]
            assert listen_calls_of(websocket, NOTIFICATION_TYPE) == [
                (NOTIFICATION_TYPE, "7", "99")
            ]

        asyncio.run(run())

    def test_both_types_use_the_existing_operator_user_id(self):
        """FR-016: `user:read:chat` already covers both, so nothing reseeds."""

        async def run():
            pool = make_dual_pool()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            assert {call[2] for call in websocket.listen_calls} == {"99"}

        asyncio.run(run())

    def test_each_type_gets_its_own_callback(self):
        async def run():
            chat_events = []
            notification_events = []

            async def on_chat(event):
                chat_events.append(event)

            async def on_notification(event):
                notification_events.append(event)

            pool = make_dual_pool(handler=on_chat, notification_handler=on_notification)
            await pool.create(7)
            websocket = pool._connections[0].websocket

            notification_id = notification_slot(pool, 7).subscription_id
            chat_id = chat_slot(pool, 7).subscription_id
            assert (
                websocket._callbacks[notification_id]["callback"]
                != websocket._callbacks[chat_id]["callback"]
            ), "both types were registered against the chat handler"

            await websocket._callbacks[notification_id]["callback"](
                make_notification_event(broadcaster_id=7)
            )
            assert len(notification_events) == 1
            assert chat_events == [], "a notification reached the chat publisher"

        asyncio.run(run())

    def test_repairing_chat_only_creates_only_the_notification(self):
        async def run():
            pool = make_dual_pool()
            await pool._grow()
            await make_chat_only(pool, 7)
            websocket = pool._connections[0].websocket
            surviving_id = chat_slot(pool, 7).subscription_id
            calls_before = len(websocket.listen_calls)

            await pool.create(7)

            assert len(listen_calls_of(websocket, CHAT_TYPE)) == 1, (
                "the surviving chat subscription was duplicated"
            )
            assert chat_slot(pool, 7).subscription_id == surviving_id
            assert len(websocket.listen_calls) == calls_before + 1
            assert websocket.listen_calls[-1][0] == NOTIFICATION_TYPE
            assert coverage_state(pool, 7) == "complete"
            assert pool.occupancy() == {"0": 2}

        asyncio.run(run())

    def test_repairing_notification_only_creates_only_the_chat(self):
        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            surviving_id = notification_slot(pool, 7).subscription_id

            await pool._on_revocation({
                "subscription": {
                    "id": chat_slot(pool, 7).subscription_id,
                    "type": CHAT_TYPE,
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "7"},
                }
            })
            await asyncio.sleep(0)
            assert coverage_state(pool, 7) == "notification_only"

            await pool.create(7)

            assert notification_slot(pool, 7).subscription_id == surviving_id
            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 1
            assert len(listen_calls_of(websocket, CHAT_TYPE)) == 2
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_both_slots_are_stamped_with_the_session_they_were_made_on(self):
        async def run():
            pool = make_dual_pool()
            await pool.create(7)
            session = pool._connections[0].websocket.session_id

            assert chat_slot(pool, 7).session_id == session
            assert notification_slot(pool, 7).session_id == session

        asyncio.run(run())

    def test_each_call_keeps_the_session_read_before_that_call(self):
        """The stamp is per subscription, not per channel.

        The library builds each POST's transport from whatever session is
        current when THAT request goes out, so a reconnect between the two
        listen calls makes one half honest and the other a lie. Stamping both
        from a single reading -- before or after -- records a subscription on
        a session it was never made on, and `_slot_is_current` then agrees
        with itself for ever while the channel is dark.
        """

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.start()
            connection = await pool._grow()
            websocket = connection.websocket
            first_session = websocket.session_id
            original = websocket.listen_channel_chat_notification

            async def reconnect_during_notification(broadcaster_user_id, user_id, callback):
                subscription_id = await original(broadcaster_user_id, user_id, callback)
                websocket.reconnect("session-after-reconnect")
                return subscription_id

            websocket.listen_channel_chat_notification = reconnect_during_notification

            with pytest.raises(TransportError):
                await pool.create(7)

            assert notification_slot(pool, 7) is None, (
                "a subscription whose session changed under it was recorded"
            )
            assert twitch.deleted, "the discarded auxiliary was never deleted"
            chat = chat_slot(pool, 7)
            # Whether the chat half is kept for the next pass or dropped with
            # its sibling is the implementation's call. What it may NEVER do
            # is relabel it with a session it was not made on: that stamp is
            # the only check `_slot_is_current` cannot be talked out of.
            assert chat is None or chat.session_id == first_session
            assert connection.reserved == 0

        asyncio.run(run())


class TestPoolPairEnumeration:
    """T011 / R1 -- `list()` joins two walks, and either failure is a hole."""

    @staticmethod
    async def _complete_pair_rows(pool, broadcaster_ids):
        session = pool._connections[0].websocket.session_id
        rows = []
        for broadcaster_id in broadcaster_ids:
            rows.append(
                existing_subscription(
                    chat_slot(pool, broadcaster_id).subscription_id,
                    broadcaster_id,
                    session,
                )
            )
            rows.append(
                notification_subscription(
                    notification_slot(pool, broadcaster_id).subscription_id,
                    broadcaster_id,
                    session,
                )
            )
        return rows

    def test_list_walks_both_types_and_joins_them_per_channel(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.create(1)
            await pool.create(2)
            twitch.subscriptions = await self._complete_pair_rows(pool, [1, 2])

            seen = [sub async for sub in pool.list()]

            assert {CHAT_TYPE, NOTIFICATION_TYPE} <= set(twitch.listed_types)
            assert {sub.broadcaster_id for sub in seen} == {1, 2}
            assert len(seen) == 2, "a channel was yielded once per subscription"
            assert {sub.subscription_id for sub in seen} == {
                chat_slot(pool, 1).subscription_id,
                chat_slot(pool, 2).subscription_id,
            }

        asyncio.run(run())

    def test_a_channel_with_only_one_type_is_not_complete(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.create(1)
            await pool.create(2)
            rows = await self._complete_pair_rows(pool, [1, 2])
            # Channel 2 lost its auxiliary half on Twitch's side.
            twitch.subscriptions = [
                row
                for row in rows
                if not (
                    row.condition["broadcaster_user_id"] == "2"
                    and row.type == NOTIFICATION_TYPE
                )
            ]

            seen = [sub async for sub in pool.list()]

            assert [sub.broadcaster_id for sub in seen] == [1]

        asyncio.run(run())

    def test_a_type_that_is_not_enabled_does_not_complete_a_pair(self):
        """`ADOPTABLE_STATUSES` is `enabled` only, and it has to hold for
        BOTH halves or a revoked auxiliary reads as covered."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.create(1)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription(
                    chat_slot(pool, 1).subscription_id, 1, session
                ),
                notification_subscription(
                    notification_slot(pool, 1).subscription_id,
                    1,
                    session,
                    status="authorization_revoked",
                ),
            ]

            seen = [sub async for sub in pool.list()]
            assert seen == [], (
                "a pair whose auxiliary half is revoked was reported as adoptable"
            )

        asyncio.run(run())

    def test_a_half_on_a_foreign_session_does_not_complete_a_pair(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.create(1)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription(chat_slot(pool, 1).subscription_id, 1, session),
                notification_subscription("n-1", 1, "a-session-from-a-dead-process"),
            ]

            seen = [sub async for sub in pool.list()]
            assert seen == []

        asyncio.run(run())

    def test_a_degraded_channel_is_reported_as_actual(self):
        """Otherwise the reconciler drops it from `_actual` every pass and
        re-creates a subscription Twitch has just refused -- the hot loop the
        bounded hold-off exists to stop."""

        async def run():
            twitch = FakePoolTwitch()
            clock = FakeMonotonicMs()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription(chat_slot(pool, 7).subscription_id, 7, session)
            ]

            seen = [sub async for sub in pool.list()]

            assert [sub.broadcaster_id for sub in seen] == [7]
            assert seen[0].subscription_id == chat_slot(pool, 7).subscription_id
            assert coverage_state(pool, 7) == "degraded_chat_only"

        asyncio.run(run())

    def test_a_plain_partial_channel_is_not_reported_as_actual(self):
        """`chat_only` without a hold-off must stay out, so ordinary repair
        runs on the next pass."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool._grow()
            await make_chat_only(pool, 7)
            session = pool._connections[0].websocket.session_id
            twitch.subscriptions = [
                existing_subscription(chat_slot(pool, 7).subscription_id, 7, session)
            ]

            assert [sub async for sub in pool.list()] == []

        asyncio.run(run())

    @pytest.mark.parametrize("failing_type", [CHAT_TYPE, NOTIFICATION_TYPE])
    def test_either_walk_failing_makes_the_enumeration_incomplete(self, failing_type):
        """R1. "Clean" must mean BOTH walks finished, or the reconciler deletes
        live subscriptions it merely failed to see."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.create(1)
            twitch.subscriptions = await self._complete_pair_rows(pool, [1])
            twitch.list_errors[failing_type] = eventsub_pool.TwitchAPIException(
                "helix 500"
            )

            with pytest.raises(Exception):
                [sub async for sub in pool.list()]

        asyncio.run(run())

    @pytest.mark.parametrize("failing_type", [CHAT_TYPE, NOTIFICATION_TYPE])
    def test_a_failed_walk_holds_the_reconciler_s_drops_back(self, failing_type):
        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)
            seed_desired(fake_redis, [("c1", 1)])

            await reconciler.reconcile_once()
            assert reconciler._adoption_complete is True

            twitch.subscriptions = await self._complete_pair_rows(pool, [1])
            twitch.list_errors[failing_type] = eventsub_pool.TwitchAPIException(
                "helix 500"
            )
            # The channel leaves the desired set: without a clean walk the
            # reconciler must NOT act on that.
            seed_desired(fake_redis, [])
            await reconciler.reconcile_once()

            assert reconciler._adoption_complete is False
            assert twitch.deleted == [], (
                "a live pair was deleted on the strength of a half-finished walk"
            )

        asyncio.run(run())

    def test_a_walk_that_fails_part_way_keeps_what_it_saw(self):
        """The channels seen before the failure are still real. What must not
        happen is the enumeration reporting itself clean."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            await pool.create(1)
            await pool.create(2)
            twitch.subscriptions = await self._complete_pair_rows(pool, [1, 2])
            twitch.list_errors_after[NOTIFICATION_TYPE] = 1

            seen = []
            with pytest.raises(Exception):
                async for subscription in pool.list():
                    seen.append(subscription)

            assert {sub.broadcaster_id for sub in seen} <= {1}, (
                "a channel was completed from a walk that never reached its "
                "auxiliary half"
            )

        asyncio.run(run())


class TestPoolPairAdoption:
    """T012 / FR-005 -- a 409 adopts the matching (type, broadcaster) only."""

    def test_a_chat_conflict_adopts_the_chat_subscription(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            connection = await pool._grow()
            session = connection.websocket.session_id
            twitch.subscriptions = [
                existing_subscription("adopted-chat", 7, session),
                notification_subscription("someone-elses-notice", 8, session),
            ]
            connection.websocket.raise_on_subscribe_by_type[CHAT_TYPE] = (
                eventsub_pool.EventSubSubscriptionConflict("409 conflict")
            )

            await pool.create(7)

            assert chat_slot(pool, 7).subscription_id == "adopted-chat"
            assert notification_slot(pool, 7) is not None
            assert notification_slot(pool, 7).subscription_id != "someone-elses-notice"
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_a_notification_conflict_adopts_the_notification_subscription(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            connection = await pool._grow()
            session = connection.websocket.session_id
            twitch.subscriptions = [
                notification_subscription("adopted-notice", 7, session),
                existing_subscription("a-chat-row-for-7", 7, session),
            ]
            connection.websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = (
                eventsub_pool.EventSubSubscriptionConflict("409 conflict")
            )

            await pool.create(7)

            assert notification_slot(pool, 7).subscription_id == "adopted-notice"
            assert chat_slot(pool, 7).subscription_id != "a-chat-row-for-7", (
                "the 409 for the auxiliary type adopted a chat subscription"
            )
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_a_conflict_never_adopts_the_other_type(self):
        """Only a chat row exists, and the auxiliary create conflicted. There
        is nothing legitimate to adopt, so the channel stays incomplete."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            connection = await pool._grow()
            session = connection.websocket.session_id
            twitch.subscriptions = [existing_subscription("chat-only-row", 7, session)]
            connection.websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = (
                eventsub_pool.EventSubSubscriptionConflict("409 conflict")
            )

            with pytest.raises(TransportError):
                await pool.create(7)

            assert notification_slot(pool, 7) is None
            assert coverage_state(pool, 7) == "chat_only"

        asyncio.run(run())

    def test_a_conflict_never_adopts_another_broadcaster(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            connection = await pool._grow()
            session = connection.websocket.session_id
            twitch.subscriptions = [notification_subscription("notice-for-8", 8, session)]
            connection.websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = (
                eventsub_pool.EventSubSubscriptionConflict("409 conflict")
            )

            with pytest.raises(TransportError):
                await pool.create(7)
            assert notification_slot(pool, 7) is None

        asyncio.run(run())

    @pytest.mark.parametrize("conflicting_type", [CHAT_TYPE, NOTIFICATION_TYPE])
    def test_a_conflict_refuses_a_subscription_on_a_session_we_do_not_hold(
        self, conflicting_type
    ):
        """A websocket session dies with the process that opened it. Claiming
        one this pool does not hold counts a dark channel as covered."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            connection = await pool._grow()
            builder = (
                existing_subscription
                if conflicting_type == CHAT_TYPE
                else notification_subscription
            )
            twitch.subscriptions = [builder("ghost", 7, "a-session-from-a-dead-process")]
            connection.websocket.raise_on_subscribe_by_type[conflicting_type] = (
                eventsub_pool.EventSubSubscriptionConflict("409 conflict")
            )

            with pytest.raises(TransportError):
                await pool.create(7)

            assert "ghost" not in pool._by_subscription
            assert coverage_state(pool, 7) != "complete"

        asyncio.run(run())


class TestPoolPairDeletes:
    """T013 -- dropping a channel means dropping both of its subscriptions."""

    def test_dropping_a_channel_deletes_both_types(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            handle = await pool.create(7)
            expected = {
                chat_slot(pool, 7).subscription_id,
                notification_slot(pool, 7).subscription_id,
            }

            await pool.delete(handle)

            assert set(twitch.deleted) == expected
            assert pool._slots == {}
            assert pool._by_subscription == {}
            assert pool.occupancy() == {"0": 0}
            assert coverage_state(pool, 7) == "absent"

        asyncio.run(run())

    @pytest.mark.parametrize("gone_type", [CHAT_TYPE, NOTIFICATION_TYPE])
    def test_an_already_gone_half_is_success(self, gone_type):
        """A `websocket_disconnected` leftover answers "not found" on DELETE.
        That is success for either half, not a failure that strands the other."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            handle = await pool.create(7)
            slot = (
                chat_slot(pool, 7)
                if gone_type == CHAT_TYPE
                else notification_slot(pool, 7)
            )
            twitch.not_found.add(slot.subscription_id)

            await pool.delete(handle)  # must not raise

            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_delete_follows_the_ids_a_reconnect_rotated_per_type(self):
        """Both ids change, and each type's delete must follow its OWN new id
        -- resolving by broadcaster alone deletes one twice and leaks the other."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            handle = await pool.create(7)
            websocket = pool._connections[0].websocket
            websocket.rotate_ids()
            live = set(websocket._active_subscriptions)
            assert len(live) == 2
            assert len(websocket.subscriptions_of_type(CHAT_TYPE)) == 1
            assert len(websocket.subscriptions_of_type(NOTIFICATION_TYPE)) == 1

            await pool.delete(handle)

            assert set(twitch.deleted) == live
            assert len(twitch.deleted) == 2
            assert websocket._active_subscriptions == {}
            assert websocket._callbacks == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_a_one_sided_delete_failure_leaves_the_survivor_retryable(self):
        """Both halves are attempted. The one that failed keeps its slot and
        its place in the occupancy count, so the next pass retries exactly it."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            handle = await pool.create(7)
            chat_id = chat_slot(pool, 7).subscription_id
            notification_id = notification_slot(pool, 7).subscription_id
            twitch.delete_errors[notification_id] = eventsub_pool.TwitchAPIException(
                "twitch 503"
            )

            with pytest.raises(TransportError):
                await pool.delete(handle)

            assert chat_id in twitch.deleted, "one failure aborted the other delete"
            assert chat_slot(pool, 7) is None
            assert notification_slot(pool, 7) is not None
            assert notification_slot(pool, 7).subscription_id == notification_id
            assert coverage_state(pool, 7) == "notification_only"
            assert pool.occupancy() == {"0": 1}

            # And the retry finishes the job.
            twitch.delete_errors.clear()
            await pool.delete(notification_id)
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}

        asyncio.run(run())

    def test_deleting_one_channel_does_not_touch_another(self):
        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            handle = await pool.create(7)
            await pool.create(8)

            await pool.delete(handle)

            assert coverage_state(pool, 8) == "complete"
            assert pool.occupancy() == {"0": 2}

        asyncio.run(run())


class TestPoolPairRevocation:
    """T014 -- a revocation costs one subscription, never the pair."""

    @staticmethod
    async def _revoke(pool, subscription_id, subscription_type, broadcaster_id):
        await pool._on_revocation({
            "subscription": {
                "id": subscription_id,
                "type": subscription_type,
                "status": "authorization_revoked",
                "condition": {"broadcaster_user_id": str(broadcaster_id)},
            }
        })
        await asyncio.sleep(0)

    def test_a_revoked_notification_leaves_the_chat_slot(self):
        async def run():
            lost = []
            pool = make_dual_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            chat_id = chat_slot(pool, 7).subscription_id

            await self._revoke(
                pool, notification_slot(pool, 7).subscription_id, NOTIFICATION_TYPE, 7
            )

            assert lost == [1], "the pair was reported lost, not the subscription"
            assert chat_slot(pool, 7).subscription_id == chat_id
            assert notification_slot(pool, 7) is None
            assert coverage_state(pool, 7) == "chat_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_a_revoked_chat_leaves_the_notification_slot(self):
        async def run():
            lost = []
            pool = make_dual_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            notification_id = notification_slot(pool, 7).subscription_id

            await self._revoke(pool, chat_slot(pool, 7).subscription_id, CHAT_TYPE, 7)

            assert lost == [1]
            assert notification_slot(pool, 7).subscription_id == notification_id
            assert chat_slot(pool, 7) is None
            assert coverage_state(pool, 7) == "notification_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    @pytest.mark.parametrize(
        "revoked_type,survivor_getter",
        [
            (NOTIFICATION_TYPE, chat_slot),
            (CHAT_TYPE, notification_slot),
        ],
    )
    def test_a_rotated_unknown_id_still_resolves_channel_and_type(
        self, revoked_type, survivor_getter
    ):
        """`_handle_revocation` empties the library registries before calling
        us, and a reconnect has already rotated both ids, so the channel AND
        the type have to come out of the payload."""

        async def run():
            lost = []
            pool = make_dual_pool(on_subscriptions_lost=lost.append)
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            survivor = survivor_getter(pool, 7).subscription_id
            websocket._active_subscriptions.clear()
            websocket._callbacks.clear()

            await self._revoke(pool, "an-id-we-never-recorded", revoked_type, 7)

            assert lost == [1]
            assert survivor_getter(pool, 7).subscription_id == survivor, (
                "the revocation removed the wrong half of the pair"
            )
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_the_partial_state_a_revocation_leaves_is_repairable(self):
        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            chat_id = chat_slot(pool, 7).subscription_id

            await self._revoke(
                pool, notification_slot(pool, 7).subscription_id, NOTIFICATION_TYPE, 7
            )
            await pool.create(7)

            assert coverage_state(pool, 7) == "complete"
            assert chat_slot(pool, 7).subscription_id == chat_id
            assert len(listen_calls_of(websocket, CHAT_TYPE)) == 1
            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 2

        asyncio.run(run())


class TestPoolPairReconnect:
    """T015 -- staleness, socket death and retirement, per type."""

    def test_chat_only_does_not_satisfy_the_notification_type(self):
        """The false positive that makes everything else look healthy: match
        on `broadcaster_user_id` alone and a channel that holds only chat
        reads as "current" for the auxiliary type, so it is never repaired."""

        async def run():
            pool = make_dual_pool()
            connection = await pool._grow()
            await make_chat_only(pool, 7, connection=connection)

            assert (
                pool._connection_holds(connection, 7, eventsub_pool.CoverageType.CHAT)
                is True
            )
            assert (
                pool._connection_holds(
                    connection, 7, eventsub_pool.CoverageType.NOTIFICATION
                )
                is False
            ), "a chat subscription answered for the notification type"

        asyncio.run(run())

    def test_a_registry_that_lost_only_one_type_recreates_only_that_type(self):
        """`_resubscribe()` can give up part way, leaving one half real and
        the other a ghost. Each half is checked on its own."""

        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            chat_id = chat_slot(pool, 7).subscription_id
            ghost = notification_slot(pool, 7).subscription_id
            websocket._active_subscriptions.pop(ghost)
            websocket._callbacks.pop(ghost)

            await pool.create(7)

            assert chat_slot(pool, 7).subscription_id == chat_id
            assert notification_slot(pool, 7).subscription_id != ghost
            assert len(listen_calls_of(websocket, CHAT_TYPE)) == 1
            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 2
            assert pool.occupancy() == {"0": 2}

        asyncio.run(run())

    def test_a_reconnect_makes_both_stamps_stale(self):
        """A new session means Twitch holds nothing from the old one, whatever
        either registry still claims."""

        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool.create(7)
            websocket = pool._connections[0].websocket
            before = {
                chat_slot(pool, 7).subscription_id,
                notification_slot(pool, 7).subscription_id,
            }
            websocket.reconnect("session-after-reconnect")

            await pool.create(7)

            after = {
                chat_slot(pool, 7).subscription_id,
                notification_slot(pool, 7).subscription_id,
            }
            assert after.isdisjoint(before), "a pre-reconnect id was handed back"
            assert len(listen_calls_of(websocket, CHAT_TYPE)) == 2
            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 2
            assert pool.occupancy() == {"0": 2}

        asyncio.run(run())

    def test_a_dead_socket_takes_both_halves_of_a_co_located_pair(self):
        async def run():
            lost = []
            pool = make_dual_pool(on_subscriptions_lost=lost.append)
            for broadcaster_id in (1, 2, 3):
                await pool.create(broadcaster_id)
            pool._connections[0].websocket.die()

            assert pool.reap_dead_connections() == 6, (
                "the loss was counted in channels, not subscriptions"
            )
            assert lost == [6]
            assert pool._slots == {}
            assert pool.occupancy() == {}
            assert coverage_state(pool, 1) == "absent"

        asyncio.run(run())

    def test_a_dead_socket_takes_only_its_own_half_of_a_split_pair(self):
        """R2's residual, made explicit: the surviving half stays, and the
        channel converges through the ordinary partial-state repair."""

        async def run():
            lost = []
            pool = make_dual_pool(cap=3, on_subscriptions_lost=lost.append)
            first = await pool._grow()
            await make_chat_only(pool, 7, connection=first)
            await pool.create(8)
            await pool.create(7)  # the auxiliary half splits onto connection 1
            second = pool._connections[1]
            assert notification_slot(pool, 7).connection_id == second.connection_id

            second.websocket.die()
            assert pool.reap_dead_connections() == 1
            assert lost == [1]

            assert chat_slot(pool, 7) is not None
            assert notification_slot(pool, 7) is None
            assert coverage_state(pool, 7) == "chat_only"
            assert coverage_state(pool, 8) == "complete"

        asyncio.run(run())

    def test_retirement_clears_both_halves_on_that_connection_only(self):
        async def run():
            pool = make_dual_pool(cap=3)
            first = await pool._grow()
            await make_chat_only(pool, 7, connection=first)
            await pool.create(8)
            await pool.create(7)
            second = pool._connections[1]

            pool._retire(first)

            assert chat_slot(pool, 7) is None
            assert coverage_state(pool, 8) == "absent"
            assert notification_slot(pool, 7) is not None, (
                "_retire deleted a slot that lives on another connection"
            )
            assert coverage_state(pool, 7) == "notification_only"
            assert pool.occupancy() == {str(second.connection_id): 1}

        asyncio.run(run())

    def test_a_mid_ramp_reconnect_never_oversubscribes(self):
        """The repair after a reconnect re-creates real subscriptions, so it
        has to be routed and reserved like any other create."""

        async def run():
            pool = make_dual_pool(cap=6, max_connections=6)
            for broadcaster_id in range(1, 13):
                await pool.create(broadcaster_id)
            assert sum(pool.occupancy().values()) == 24

            pool._connections[0].websocket.reconnect("session-mid-ramp")
            for broadcaster_id in range(1, 13):
                await pool.create(broadcaster_id)

            counts = pool.occupancy()
            assert all(count <= 6 for count in counts.values()), (
                f"a session went past its cap during the repair: {counts}"
            )
            assert sum(counts.values()) == 24
            assert all(
                coverage_state(pool, broadcaster_id) == "complete"
                for broadcaster_id in range(1, 13)
            )
            assert all(connection.reserved == 0 for connection in pool._connections)

        asyncio.run(run())


class TestPoolAuxiliaryRefusal:
    """T016 / D2 -- a bounded, pool-local hold-off, and nothing wider."""

    def test_the_hold_off_is_an_hour(self):
        assert eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS == 3600

    def test_a_notification_refusal_does_not_refuse_the_channel(self):
        """`streamers.eventsub_refused_at` is per CHANNEL and lasts seven
        days. Letting an auxiliary 403 reach the reconciler as a refusal would
        kill that channel's chat for a week to protect a suppression signal."""

        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            await pool._grow()

            handle = await make_degraded(pool, 7)  # must not raise

            assert handle == chat_slot(pool, 7).subscription_id
            assert coverage_state(pool, 7) == "degraded_chat_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_a_chat_refusal_is_still_a_channel_refusal(self):
        """The distinction has to survive: chat is the data path."""

        async def run():
            pool = make_dual_pool()
            connection = await pool._grow()
            connection.websocket.raise_on_subscribe_by_type[CHAT_TYPE] = refusal_error()

            with pytest.raises(SubscriptionRefusedError):
                await pool.create(7)

        asyncio.run(run())

    def test_the_hold_off_suppresses_further_notification_creates(self):
        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            connection = await pool._grow()
            await make_degraded(pool, 7)
            websocket = connection.websocket
            attempts = len(listen_calls_of(websocket, NOTIFICATION_TYPE))

            clock.advance_seconds(1800)
            for _ in range(10):
                await pool.create(7)

            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == attempts, (
                "the reconciler hot-looped on a refusal it cannot fix"
            )
            assert len(listen_calls_of(websocket, CHAT_TYPE)) == 1
            assert coverage_state(pool, 7) == "degraded_chat_only"

        asyncio.run(run())

    def test_the_hold_off_lasts_exactly_the_configured_hour(self):
        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            connection = await pool._grow()
            await make_degraded(pool, 7)
            websocket = connection.websocket

            clock.advance_seconds(eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS - 1)
            await pool.create(7)
            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 0, (
                "the hold-off expired early"
            )
            assert coverage_state(pool, 7) == "degraded_chat_only"

            clock.advance_seconds(1)
            assert coverage_state(pool, 7) == "chat_only", (
                "an expired hold-off still reported the channel as actual"
            )
            await pool.create(7)

            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 1
            assert coverage_state(pool, 7) == "complete"
            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None

        asyncio.run(run())

    def test_a_reconnect_makes_the_channel_repairable_at_once(self):
        """The refusal may have been specific to that session, and a reconnect
        is a free opportunity to retest it."""

        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            connection = await pool._grow()
            await make_degraded(pool, 7)

            connection.websocket.reconnect("session-after-reconnect")
            clock.advance_seconds(5)
            await pool.create(7)

            assert coverage_state(pool, 7) == "complete"
            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None

        asyncio.run(run())

    def test_retiring_the_connection_makes_the_channel_repairable_at_once(self):
        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            connection = await pool._grow()
            await make_degraded(pool, 7)

            pool._retire(connection)

            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None
            clock.advance_seconds(5)
            await pool.create(7)
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_a_successful_creation_clears_the_state(self):
        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)

            clock.advance_seconds(eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS)
            await pool.create(7)

            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_a_409_adoption_clears_the_state(self):
        async def run():
            clock = FakeMonotonicMs()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            connection = await pool._grow()
            await make_degraded(pool, 7)

            clock.advance_seconds(eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS)
            twitch.subscriptions = [
                notification_subscription(
                    "adopted-notice", 7, connection.websocket.session_id
                )
            ]
            connection.websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = (
                eventsub_pool.EventSubSubscriptionConflict("409 conflict")
            )

            await pool.create(7)

            assert notification_slot(pool, 7).subscription_id == "adopted-notice"
            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None
            assert coverage_state(pool, 7) == "complete"

        asyncio.run(run())

    def test_a_refusal_after_expiry_starts_one_more_bounded_period(self):
        """Never permanent: each refusal buys exactly one more hour."""

        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)

            clock.advance_seconds(eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS)
            await make_degraded(pool, 7)

            assert coverage_state(pool, 7) == "degraded_chat_only"
            assert pool.channel_coverage(7).auxiliary_refused_until_ms == (
                clock.now_ms + eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS * 1000
            )

        asyncio.run(run())

    def test_a_refusal_on_one_channel_does_not_hold_off_another(self):
        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)

            await pool.create(8)

            assert coverage_state(pool, 8) == "complete"
            assert pool.channel_coverage(8).auxiliary_refused_until_ms is None

        asyncio.run(run())

    def test_a_refused_channel_keeps_its_chat_subscription(self):
        """The whole point of D2: no eviction, no data loss."""

        async def run():
            clock = FakeMonotonicMs()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)

            assert chat_slot(pool, 7) is not None
            assert twitch.deleted == [], "the auxiliary refusal evicted live chat"

        asyncio.run(run())


async def revoke_subscription(pool, subscription_id, subscription_type, broadcaster_id):
    """Deliver one Twitch revocation exactly as the library delivers it.

    Shared by the pair-revocation tests and the reclamation tests below: the
    payload carries the subscription object, condition and type included,
    because the library has already emptied its own registries by the time the
    handler runs.
    """
    await pool._on_revocation({
        "subscription": {
            "id": subscription_id,
            "type": subscription_type,
            "status": "authorization_revoked",
            "condition": {"broadcaster_user_id": str(broadcaster_id)},
        }
    })
    # `_on_revocation` hops to the service loop with `call_soon_threadsafe`.
    await asyncio.sleep(0)


def rotated_id_of(websocket, subscription_type):
    """The single live id the library now holds for one coverage type."""
    ids = websocket.subscriptions_of_type(subscription_type)
    assert len(ids) == 1, f"expected one live {subscription_type}, got {ids}"
    return next(iter(ids))


class TestPoolRotatedHandleDeletes:
    """T013 / T021 / FR-002, I3 -- a drop whose handle is a ROTATED id.

    `delete()` resolves the channel through `_by_subscription`, and that index
    holds the ids recorded at CREATE time. A library reconnect resubscribes
    the whole socket and rotates BOTH ids, and the very next enumeration hands
    the reconciler the rotated CHAT id -- so `_drop_one` calls `delete()` with
    an id the pool's own indexes have never seen. That is the unrecognised-id
    branch, and it deletes exactly ONE subscription: the sibling's rotated id
    stays live on Twitch, in the library's registry and in the occupancy
    count, and the channel is left as a `notification_only` orphan that
    nothing will ever drop -- the channel is gone from the desired set, so no
    later pass creates it, and `list()` never yields a partial channel, so no
    later pass drops it either.

    `test_delete_follows_the_ids_a_reconnect_rotated_per_type` above covers
    the case where the handle is still one the pool recorded. This is the case
    where it is not, which is the one the reconciler actually produces.
    """

    def test_deleting_a_rotated_handle_drops_both_halves(self):
        """One `delete()` call, with the id the reconciler would be holding."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            handle = await pool.create(7)
            websocket = pool._connections[0].websocket

            # The reconnect the library performs on its own: same channels,
            # new ids, and the pool's indexes are not told.
            websocket.rotate_ids()
            rotated = set(websocket._active_subscriptions)
            rotated_chat = rotated_id_of(websocket, CHAT_TYPE)
            rotated_notification = rotated_id_of(websocket, NOTIFICATION_TYPE)
            assert handle not in rotated, "the handle was not actually rotated"
            assert pool._by_subscription.keys().isdisjoint(rotated), (
                "the pool's indexes were refreshed, so this is not the case "
                "under test"
            )

            # Exactly what `Reconciler._drop_one` does: one call, with the
            # handle the last enumeration reported.
            await pool.delete(rotated_chat)

            assert set(twitch.deleted) == {rotated_chat, rotated_notification}, (
                "the rotated sibling was left live on Twitch"
            )
            assert len(twitch.deleted) == 2, "a subscription was deleted twice"
            assert websocket._active_subscriptions == {}, (
                "the library will resubscribe the orphan on its next reconnect"
            )
            assert websocket._callbacks == {}
            assert chat_slot(pool, 7) is None
            assert notification_slot(pool, 7) is None
            assert pool._slots == {}
            assert pool._by_subscription == {}
            assert pool.occupancy() == {"0": 0}
            assert coverage_state(pool, 7) == "absent"
            assert pool.coverage_counts() == {
                state: 0 for state in eventsub_pool.COVERAGE_STATES
            }

        asyncio.run(run())

    def test_a_reconciler_drop_after_a_reconnect_leaves_no_orphan(self):
        """The same defect through the deployed control flow.

        The reconciler re-enumerates, so `_actual` holds the ROTATED chat id;
        the pool's indexes still hold the pre-reconnect pair. The drop then
        goes down the unrecognised-id branch.
        """

        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)

            seed_desired(fake_redis, [("c7", 7)])
            await reconciler.reconcile_once()
            assert coverage_state(pool, 7) == "complete"

            websocket = pool._connections[0].websocket
            websocket.rotate_ids()
            rotated_chat = rotated_id_of(websocket, CHAT_TYPE)
            rotated_notification = rotated_id_of(websocket, NOTIFICATION_TYPE)
            session = websocket.session_id
            twitch.subscriptions = [
                existing_subscription(rotated_chat, 7, session),
                notification_subscription(rotated_notification, 7, session),
            ]

            seed_desired(fake_redis, [])
            await reconciler.reconcile_once()

            assert reconciler._actual == {}
            assert set(twitch.deleted) == {rotated_chat, rotated_notification}, (
                "the reconciler's drop left the auxiliary half live"
            )
            assert len(twitch.deleted) == 2
            assert websocket._active_subscriptions == {}
            assert websocket._callbacks == {}
            assert pool._slots == {}
            assert pool._by_subscription == {}
            assert pool.occupancy() == {"0": 0}
            assert coverage_state(pool, 7) == "absent"
            assert pool.coverage_counts()["notification_only"] == 0, (
                "the drop left a notification_only orphan behind"
            )

        asyncio.run(run())


class TestUndesiredPartialChannelReclamation:
    """T013 / T021 / FR-001, NFR-003 -- a partial channel nobody wants.

    A create that lands one half and fails the other raises, so the channel
    never enters the reconciler's `_actual`. `list()` deliberately does not
    yield a partial channel -- that is what makes the next pass repair the
    missing half -- so while the channel is still DESIRED the state converges.

    It does not converge once the channel leaves the desired set. It is in
    nothing the reconciler diffs: not in `_actual`, so never in `to_drop`; not
    in `desired`, so never in `to_create`. The surviving subscription then
    stays live for the life of the process, holding one of the 300 slots on
    its session and delivering chat for a channel nobody is monitoring
    (FR-013/FR-014 capacity, NFR-003 convergence).

    The reclamation needs a handle the transport can be asked for, because
    `list()` must NOT start reporting partial channels as actual -- that would
    stop the repair path dead. These tests are written against the minimal
    explicit API that gives it: `partial_channel_handles()`, drop-only.
    """

    @staticmethod
    async def _partial_create(pool, reconciler, fake_redis, broadcaster_id):
        """Chat succeeds, the auxiliary half hits a transient transport error.

        Driven through `reconcile_once()`, so the channel is left exactly as
        the deployed path leaves it: covered by chat on Twitch, and unknown to
        the reconciler because `create()` raised.
        """
        connection = pool._connections[0] if pool._connections else await pool._grow()
        websocket = connection.websocket
        websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = (
            eventsub_pool.TwitchBackendException("twitch 500")
        )
        try:
            seed_desired(fake_redis, [(f"c{broadcaster_id}", broadcaster_id)])
            await reconciler.reconcile_once()
        finally:
            websocket.raise_on_subscribe_by_type.pop(NOTIFICATION_TYPE, None)
        return connection

    def test_an_undesired_chat_only_partial_is_reclaimed_on_a_clean_pass(self):
        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)

            connection = await self._partial_create(pool, reconciler, fake_redis, 7)
            chat_id = chat_slot(pool, 7).subscription_id

            assert coverage_state(pool, 7) == "chat_only"
            assert reconciler._actual == {}, (
                "a partial channel entered the actual set, which would stop "
                "the repair path"
            )
            assert connection.reserved == 0
            assert pool.occupancy() == {"0": 1}

            # The channel leaves the desired set before anything repairs it.
            seed_desired(fake_redis, [])
            twitch.subscriptions = [
                existing_subscription(chat_id, 7, connection.websocket.session_id)
            ]

            await reconciler.reconcile_once()

            assert reconciler._adoption_complete is True, (
                "the enumeration was not clean, so this proves nothing"
            )
            assert twitch.deleted == [chat_id], (
                "the surviving chat subscription of an undesired partial "
                "channel was never reclaimed"
            )
            assert pool._slots == {}
            assert pool._by_subscription == {}
            assert pool.occupancy() == {"0": 0}
            assert coverage_state(pool, 7) == "absent"
            assert pool.coverage_counts()["chat_only"] == 0

        asyncio.run(run())

    def test_an_undesired_notification_only_partial_is_reclaimed_too(self):
        """The symmetric half. §5.4 produces it: Twitch revokes the chat
        subscription and the auxiliary one survives, so the channel is not
        actual and cannot be dropped by the ordinary diff either."""

        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)
            await pool.start()
            pool.on_subscriptions_lost = reconciler.invalidate_actual_set

            seed_desired(fake_redis, [("c7", 7)])
            await reconciler.reconcile_once()
            connection = pool._connections[0]
            chat_id = chat_slot(pool, 7).subscription_id
            notification_id = notification_slot(pool, 7).subscription_id

            await revoke_subscription(pool, chat_id, CHAT_TYPE, 7)
            assert coverage_state(pool, 7) == "notification_only"

            seed_desired(fake_redis, [])
            twitch.subscriptions = [
                notification_subscription(
                    notification_id, 7, connection.websocket.session_id
                )
            ]

            await reconciler.reconcile_once()

            assert reconciler._adoption_complete is True
            assert twitch.deleted == [notification_id], (
                "the surviving auxiliary subscription of an undesired partial "
                "channel was never reclaimed"
            )
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}
            assert coverage_state(pool, 7) == "absent"
            assert pool.coverage_counts()["notification_only"] == 0

        asyncio.run(run())

    @pytest.mark.parametrize("failing_type", [CHAT_TYPE, NOTIFICATION_TYPE])
    def test_a_partial_channel_is_never_dropped_on_an_incomplete_walk(
        self, failing_type
    ):
        """The safety invariant reclamation must not cost (NFR-003).

        "Extra" only means extra when the whole picture was seen. A half-
        finished enumeration must hold partial-channel drops back exactly as
        it holds ordinary drops back.
        """

        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)

            connection = await self._partial_create(pool, reconciler, fake_redis, 7)
            chat_id = chat_slot(pool, 7).subscription_id
            assert coverage_state(pool, 7) == "chat_only"

            seed_desired(fake_redis, [])
            twitch.subscriptions = [
                existing_subscription(chat_id, 7, connection.websocket.session_id)
            ]
            twitch.list_errors[failing_type] = eventsub_pool.TwitchAPIException(
                "helix 500"
            )

            await reconciler.reconcile_once()

            assert reconciler._adoption_complete is False
            assert twitch.deleted == [], (
                "a partial channel was reclaimed on the strength of a "
                "half-finished walk"
            )
            assert chat_slot(pool, 7) is not None
            assert coverage_state(pool, 7) == "chat_only"
            assert pool.occupancy() == {"0": 1}

        asyncio.run(run())

    def test_partial_channel_handles_offers_drop_only_handles(self):
        """The minimal explicit API, and its exact boundary.

        Only the two ordinary partial states are droppable. A `complete`
        channel and a `degraded_chat_only` one are both ACTUAL -- `list()`
        yields them, so the ordinary diff already drops them when they leave
        the desired set, and reporting them here would give the reconciler two
        routes to the same delete and would evict the live chat of a channel
        inside its bounded hold-off (I1, I17).
        """

        async def run():
            clock = FakeMonotonicMs()
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            await pool.start()
            connection = await pool._grow()
            session = connection.websocket.session_id

            await pool.create(1)                                   # complete
            await make_chat_only(pool, 2, connection=connection)   # chat_only
            await make_degraded(pool, 3, connection=connection)    # degraded
            await pool.create(4)
            notification_id = notification_slot(pool, 4).subscription_id
            await revoke_subscription(
                pool, chat_slot(pool, 4).subscription_id, CHAT_TYPE, 4
            )                                                      # notification_only

            assert coverage_state(pool, 1) == "complete"
            assert coverage_state(pool, 2) == "chat_only"
            assert coverage_state(pool, 3) == "degraded_chat_only"
            assert coverage_state(pool, 4) == "notification_only"

            handles = pool.partial_channel_handles()

            assert handles == {
                2: chat_slot(pool, 2).subscription_id,
                4: notification_id,
            }, "the drop-only handle set is not the two ordinary partial states"

            # And `list()`'s contract is untouched: only complete and
            # active-degraded channels count as actual for desired coverage.
            twitch.subscriptions = [
                existing_subscription(chat_slot(pool, 1).subscription_id, 1, session),
                notification_subscription(
                    notification_slot(pool, 1).subscription_id, 1, session
                ),
                existing_subscription(chat_slot(pool, 2).subscription_id, 2, session),
                existing_subscription(chat_slot(pool, 3).subscription_id, 3, session),
                notification_subscription(notification_id, 4, session),
            ]
            actual = {sub.broadcaster_id async for sub in pool.list()}

            assert actual == {1, 3}, (
                "list() started reporting partial channels as actual, which "
                "stops the repair path"
            )
            assert actual.isdisjoint(handles), (
                "a channel was both actual and droppable-as-partial"
            )

        asyncio.run(run())

    def test_a_partial_handle_deletes_the_channel_it_names(self):
        """Whatever type the handle belongs to, `delete()` already means
        "drop this CHANNEL", so the reclamation needs no second delete path."""

        async def run():
            twitch = FakePoolTwitch()
            pool = make_dual_pool(twitch=twitch)
            connection = await pool._grow()
            await make_chat_only(pool, 2, connection=connection)
            chat_id = chat_slot(pool, 2).subscription_id

            handles = pool.partial_channel_handles()
            assert handles == {2: chat_id}

            for broadcaster_id, handle in handles.items():
                await pool.delete(handle)
                assert coverage_state(pool, broadcaster_id) == "absent"

            assert twitch.deleted == [chat_id]
            assert pool._slots == {}
            assert pool.occupancy() == {"0": 0}
            assert pool.partial_channel_handles() == {}

        asyncio.run(run())


class TestAuxiliaryRefusalReconnectOnTheDeployedPath:
    """T016 / T024 / FR-001, NFR-003, I17 -- "immediately on reconnect".

    `_refresh_slots()` clears the hold-off when it sees a replaced session --
    but it only runs inside `create()`, and `create()` only runs for a channel
    the reconciler does NOT already hold. A degraded channel is reported as
    actual for the whole hold-off, precisely so the reconciler does not
    hot-loop on it, so the reconciler never calls `create()` for it and
    nothing on the deployed path ever notices the reconnect.

    The unit tests above prove the clause by calling `pool.create()` directly,
    which the reconciler will not do for this channel. These drive
    `Reconciler.reconcile_once()` instead, which is where I17's "immediately"
    has to hold: otherwise the channel sits degraded for the full hour after a
    reconnect that has already made it repairable.
    """

    @staticmethod
    async def _degrade_through_the_reconciler(pool, reconciler, fake_redis):
        connection = pool._connections[0] if pool._connections else await pool._grow()
        websocket = connection.websocket
        websocket.raise_on_subscribe_by_type[NOTIFICATION_TYPE] = refusal_error()
        try:
            seed_desired(fake_redis, [("c7", 7)])
            await reconciler.reconcile_once()
        finally:
            # The refusal was specific to the session that is about to go.
            websocket.raise_on_subscribe_by_type.pop(NOTIFICATION_TYPE, None)
        assert coverage_state(pool, 7) == "degraded_chat_only"
        assert reconciler._actual == {7: chat_slot(pool, 7).subscription_id}, (
            "the degraded channel was not actual, so this is not the case "
            "under test"
        )
        return connection

    @staticmethod
    def _reconnect_the_live_half(twitch, connection, session_id):
        """A websocket reconnect: new session, the library resubscribes.

        No `pool.create()` here on purpose -- the whole point is that the
        deployed path never calls it for a channel it counts as actual.
        """
        websocket = connection.websocket
        websocket.reconnect(session_id)
        websocket.rotate_ids()
        rotated_chat = rotated_id_of(websocket, CHAT_TYPE)
        twitch.subscriptions = [
            existing_subscription(rotated_chat, 7, websocket.session_id)
        ]
        return rotated_chat

    def test_a_reconnect_clears_the_hold_off_on_the_next_ordinary_pass(self):
        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            clock = FakeMonotonicMs()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)

            connection = await self._degrade_through_the_reconciler(
                pool, reconciler, fake_redis
            )
            self._reconnect_the_live_half(
                twitch, connection, "session-after-reconnect"
            )
            clock.advance_seconds(5)

            await reconciler.reconcile_once()

            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None, (
                "the reconnect did not clear the hold-off, so the channel "
                "stays degraded for the rest of the hour"
            )
            assert coverage_state(pool, 7) != "degraded_chat_only"

        asyncio.run(run())

    def test_the_reconnected_channel_is_repaired_without_waiting_out_the_hour(self):
        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            clock = FakeMonotonicMs()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)

            connection = await self._degrade_through_the_reconciler(
                pool, reconciler, fake_redis
            )
            websocket = connection.websocket
            started_ms = clock.now_ms
            assert listen_calls_of(websocket, NOTIFICATION_TYPE) == [], (
                "the refused create should have left no successful listen"
            )

            self._reconnect_the_live_half(
                twitch, connection, "session-after-reconnect"
            )
            clock.advance_seconds(5)

            await reconciler.reconcile_once()

            assert len(listen_calls_of(websocket, NOTIFICATION_TYPE)) == 1, (
                "the notification create was not attempted on the pass after "
                "the reconnect"
            )
            assert coverage_state(pool, 7) == "complete"
            assert pool.channel_coverage(7).auxiliary_refused_until_ms is None
            assert reconciler._actual[7] == chat_slot(pool, 7).subscription_id
            assert clock.now_ms - started_ms < (
                eventsub_pool.AUXILIARY_REFUSAL_RETRY_SECONDS * 1000
            ), "the repair only happened because the hour ran out"

        asyncio.run(run())

    def test_without_a_reconnect_the_hold_off_still_holds_the_pass_off(self):
        """The control. Nothing above may be achieved by weakening the
        hold-off itself: an untouched degraded channel must still suppress
        auxiliary creates and stay actual (D2, I17)."""

        async def run():
            fake_redis = FakeRedis()
            twitch = FakePoolTwitch()
            clock = FakeMonotonicMs()
            pool = make_dual_pool(twitch=twitch, monotonic_ms=clock)
            reconciler = make_reconciler(pool, fake_redis, readopt_interval_seconds=0)

            connection = await self._degrade_through_the_reconciler(
                pool, reconciler, fake_redis
            )
            twitch.subscriptions = [
                existing_subscription(
                    chat_slot(pool, 7).subscription_id,
                    7,
                    connection.websocket.session_id,
                )
            ]
            clock.advance_seconds(60)

            await reconciler.reconcile_once()

            assert coverage_state(pool, 7) == "degraded_chat_only"
            assert listen_calls_of(connection.websocket, NOTIFICATION_TYPE) == [], (
                "the reconciler hot-looped on a refusal it cannot fix"
            )
            assert reconciler._actual == {7: chat_slot(pool, 7).subscription_id}
            assert twitch.deleted == [], "a degraded channel lost its live chat"

        asyncio.run(run())


class TestPoolCapacityUnits:
    """T017 / FR-014, FR-015 -- subscriptions and channels are not the same unit."""

    def test_one_session_holds_a_hundred_and_fifty_complete_channels(self):
        async def run():
            pool = make_dual_pool()
            for broadcaster_id in range(1, 151):
                await pool.create(broadcaster_id)

            assert len(pool._connections) == 1
            assert pool.occupancy() == {"0": SUBSCRIPTIONS_PER_CONNECTION}
            assert pool.coverage_counts().get("complete") == 150

        asyncio.run(run())

    def test_the_hundred_and_fifty_first_channel_routes_onward(self):
        async def run():
            pool = make_dual_pool()
            for broadcaster_id in range(1, 152):
                await pool.create(broadcaster_id)

            assert len(pool._connections) == 2
            assert sum(pool.occupancy().values()) == 302
            assert all(
                count <= SUBSCRIPTIONS_PER_CONNECTION
                for count in pool.occupancy().values()
            )

        asyncio.run(run())

    def test_four_hundred_channels_use_eight_hundred_of_nine_hundred(self):
        async def run():
            pool = make_dual_pool()
            for broadcaster_id in range(1, 401):
                await pool.create(broadcaster_id)

            subscriptions = sum(pool.occupancy().values())
            assert subscriptions == 800
            assert eventsub_pool.MAX_SUBSCRIPTIONS == 900
            assert eventsub_pool.MAX_SUBSCRIPTIONS - subscriptions == 100, (
                "the adoption and reconnect headroom is gone"
            )
            assert len(pool._connections) <= eventsub_pool.MAX_CONNECTIONS
            assert all(
                count <= SUBSCRIPTIONS_PER_CONNECTION
                for count in pool.occupancy().values()
            )
            assert pool.coverage_counts().get("complete") == 400

        asyncio.run(run())

    def test_the_four_hundred_and_first_candidate_is_not_admitted(self):
        """T017/T027: the 400/400 intent layer never returns a 401st channel.

        The existing pool-capacity tests cover the 800-subscription runtime
        side; this pins the desired-set boundary without duplicating them.
        """

        ranked = [f"c{index}" for index in range(1, 402)]

        fresh = compute_desired_set(ranked, {}, 400, 400)
        assert len(fresh) == 400
        assert "c401" not in fresh

        retained = compute_desired_set(ranked, {"c401": 401}, 400, 400)
        assert len(retained) == 400
        assert "c401" not in retained, (
            "the previous set carried a 401st channel past the ceiling, which "
            "is 802 subscriptions"
        )

    def test_occupancy_counts_subscriptions_and_coverage_counts_channels(self):
        async def run():
            pool = make_dual_pool()
            for broadcaster_id in range(1, 11):
                await pool.create(broadcaster_id)

            assert sum(pool.occupancy().values()) == 20
            counts = pool.coverage_counts()
            assert counts.get("complete") == 10
            assert sum(counts.values()) == 10, (
                "a channel was counted once per subscription"
            )

        asyncio.run(run())

    def test_a_degraded_channel_is_one_channel_and_one_subscription(self):
        async def run():
            clock = FakeMonotonicMs()
            pool = make_dual_pool(monotonic_ms=clock)
            await pool._grow()
            await make_degraded(pool, 7)
            await pool.create(8)

            assert sum(pool.occupancy().values()) == 3
            counts = pool.coverage_counts()
            assert counts.get("degraded_chat_only") == 1
            assert counts.get("complete") == 1
            assert sum(counts.values()) == 2

        asyncio.run(run())

    def test_coverage_states_are_reported_separately(self):
        """FR-015's gauge has to say WHICH half is missing, per channel."""

        async def run():
            pool = make_dual_pool()
            await pool.start()
            await pool._grow()
            await pool.create(1)
            await make_chat_only(pool, 2)
            await pool.create(3)
            await pool._on_revocation({
                "subscription": {
                    "id": chat_slot(pool, 3).subscription_id,
                    "type": CHAT_TYPE,
                    "status": "authorization_revoked",
                    "condition": {"broadcaster_user_id": "3"},
                }
            })
            await asyncio.sleep(0)

            counts = pool.coverage_counts()
            assert counts.get("complete") == 1
            assert counts.get("chat_only") == 1
            assert counts.get("notification_only") == 1

        asyncio.run(run())


class TestDegradedWithoutUserAuth:
    """A missing token file must not crash-loop the container."""

    def test_a_missing_token_file_leaves_the_poller_running(self):
        """The pool resolves the auth user through get_users(), which an app
        token cannot do -- so building it would raise and take the container
        down in a restart loop. The warning this path logs promises the
        service keeps running with chat off, so it has to actually do that.

        This drives the real `initialize()`, because the branch under test is
        inside it and a test that re-implements the branch tests itself.
        """

        async def run():
            twitch = MagicMock()
            twitch.authenticate_app = AsyncMock()
            credentials = MagicMock()
            credentials.load.side_effect = FileNotFoundError("no token file")

            service = StreamMonitoringService()
            with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                 patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                 patch.object(stream_monitoring_service, "Producer"), \
                 patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                 patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                 patch.object(stream_monitoring_service, "start_http_server"), \
                 patch.object(StreamMonitoringService, "_build_transport", AsyncMock()) as build_transport:
                await service.initialize()

            build_transport.assert_not_called()
            assert service.transport is None
            assert service.reconciler is None
            # The poll job is still scheduled: intent keeps being written.
            assert service.scheduler.get_job("poll_streams") is not None

        asyncio.run(run())

    def test_a_bad_token_degrades_instead_of_crash_looping(self):
        """The fallback promised "running without user auth", but only
        FileNotFoundError reached it. A token that expired and cannot refresh
        raises InvalidTokenException, a scope-reduced one MissingScopeException,
        and a truncated file raises out of `credentials.load()` -- all of them
        far likelier than a missing file, and all of them used to crash-loop
        the container."""

        async def run():
            for failure in (
                ValueError("expired and could not refresh"),
                KeyError("access_token"),
            ):
                twitch = MagicMock()
                twitch.authenticate_app = AsyncMock()
                credentials = MagicMock()
                credentials.load.side_effect = failure

                service = StreamMonitoringService()
                with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                     patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                     patch.object(stream_monitoring_service, "Producer"), \
                     patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                     patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                     patch.object(stream_monitoring_service, "start_http_server"), \
                     patch.object(StreamMonitoringService, "_build_transport", AsyncMock()):
                    await service.initialize()   # must not raise

                assert service.has_user_auth is False
                assert service.scheduler.get_job("poll_streams") is not None

        asyncio.run(run())

    def test_the_metrics_server_starts_before_anything_that_can_fail(self):
        """Whatever kills start-up, /metrics has to be up first -- it is where
        the operator is sent to diagnose the failure."""

        async def run():
            service = StreamMonitoringService()
            with patch.object(
                stream_monitoring_service, "Twitch",
                AsyncMock(side_effect=RuntimeError("twitch is down")),
            ), \
                 patch.object(stream_monitoring_service, "start_http_server") as metrics:
                with pytest.raises(RuntimeError):
                    await service.initialize()

            metrics.assert_called_once()

        asyncio.run(run())

    def test_the_refresh_callback_is_registered_before_authenticating(self):
        """`set_user_authentication` refreshes internally on a 401 and invokes
        the callback during that call. Assigned afterwards it is still None at
        that moment, so the rotated refresh token is dropped and the file keeps
        the old one -- which locks the service out at the next restart."""

        async def run():
            seen = []
            twitch = MagicMock()

            async def set_user_authentication(*args, **kwargs):
                seen.append(twitch.user_auth_refresh_callback)

            twitch.set_user_authentication = set_user_authentication
            twitch.user_auth_refresh_callback = None

            credentials = MagicMock()
            record = MagicMock()
            record.access_token = "a"
            record.refresh_token = "r"
            record.scopes = ["user:read:chat", "clips:edit"]
            credentials.load.return_value = record

            service = StreamMonitoringService()
            with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                 patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                 patch.object(stream_monitoring_service, "Producer"), \
                 patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                 patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                 patch.object(stream_monitoring_service, "start_http_server"), \
                 patch.object(StreamMonitoringService, "_build_transport", AsyncMock()):
                await service.initialize()

            assert seen and seen[0] is not None, (
                "a refresh during set_user_authentication would have dropped "
                "the rotated refresh token"
            )

        asyncio.run(run())

    def test_a_transient_transport_failure_propagates_so_docker_restarts_us(self):
        """`_build_transport()` calls get_users(), so its failures are
        transient by nature. An earlier round swallowed them to avoid
        crash-looping, which was worse than the crash: the service ran for the
        rest of the process lifetime with no transport, no reconciler and zero
        chat ingestion, while the poll job kept working, /health kept returning
        OK, and nothing ever retried.

        Letting it propagate restarts the container, which recovers by itself.
        The metrics server is up before this point either way -- that was the
        fix worth keeping, and it is asserted here so the two cannot drift.
        """

        async def run():
            twitch = MagicMock()
            credentials = MagicMock()
            record = MagicMock()
            record.access_token = "a"
            record.refresh_token = "r"
            record.scopes = ["user:read:chat", "clips:edit"]
            credentials.load.return_value = record
            twitch.set_user_authentication = AsyncMock()

            service = StreamMonitoringService()
            with patch.object(stream_monitoring_service, "Twitch", AsyncMock(return_value=twitch)), \
                 patch.object(stream_monitoring_service, "get_credentials", return_value=credentials), \
                 patch.object(stream_monitoring_service, "Producer"), \
                 patch.object(stream_monitoring_service.psycopg2.pool, "ThreadedConnectionPool"), \
                 patch.object(stream_monitoring_service.redis, "from_url", return_value=MagicMock()), \
                 patch.object(stream_monitoring_service, "start_http_server") as metrics, \
                 patch.object(
                     StreamMonitoringService, "_build_transport",
                     AsyncMock(side_effect=RuntimeError("twitch 503")),
                 ):
                with pytest.raises(RuntimeError):
                    await service.start()

            metrics.assert_called_once()
            # And start-up is over even though it raised, so a shutdown racing
            # it does not wait out the full INITIALIZE_WAIT_SECONDS before
            # tearing down.
            assert service._init_task is not None and service._init_task.done()

        asyncio.run(run())

    def test_start_does_not_launch_a_reconciler_that_was_never_built(self):
        """The `if self.reconciler is not None` guard in `start()`.

        Pre-setting `running = False` no longer reaches it: `start()` now
        treats that as "shutdown was signalled" and returns before it
        initializes anything, which made this test pass whether the guard
        existed or not. Stop the keep-alive loop the way a signal does
        instead, so the guard is actually exercised.
        """

        async def run():
            service = StreamMonitoringService()

            async def fake_initialize():
                service.reconciler = None
                service.scheduler = MagicMock()

            service.initialize = fake_initialize
            starter = asyncio.create_task(service.start())
            await asyncio.sleep(0.05)
            service.running = False
            await asyncio.wait_for(starter, timeout=2)

            service.scheduler.start.assert_called_once(), "start() never got that far"
            assert service._reconciler_task is None

        asyncio.run(run())


class TestEventSubMessageMapping:
    """T020 / FR-008, FR-009 -- the payload the Flink job consumes."""

    def test_sent_at_is_epoch_milliseconds(self):
        moment = datetime(2026, 8, 28, 12, 0, 0, 500000, tzinfo=timezone.utc)
        assert to_epoch_ms(moment) == int(moment.timestamp() * 1000)
        assert to_epoch_ms(moment) % 1000 == 500

    def test_sent_at_parses_the_raw_rfc_3339_envelope(self):
        """Twitch sends up to nine fractional digits; 3.10 accepts three or six."""
        assert to_epoch_ms("2026-08-28T12:00:00.500000000Z") == to_epoch_ms(
            datetime(2026, 8, 28, 12, 0, 0, 500000, tzinfo=timezone.utc)
        )
        assert to_epoch_ms("2026-08-28T12:00:00Z") == 1787918400000
        assert to_epoch_ms(None) is None

    def test_sent_at_is_never_a_string(self):
        """Contract invariant 2. A string makes SentAtTimestampAssigner fall
        back to record time, silently, and event-time detection drifts."""
        payload = map_chat_message(make_eventsub_event())
        assert isinstance(payload["sent_at"], int)

    def test_an_envelope_without_a_timestamp_still_publishes(self):
        """`spec.md` Edge Cases: a missing or unparseable `message_timestamp`
        publishes with `sent_at` null, so the Flink assigner falls back to
        record time. It does NOT drop the message -- that would trade a field
        the contract already allows to be null for a chat message, against the
        constitution's no-data-loss rule.

        `TwitchObject.__init__` skips any field the payload omits, so an
        envelope without the timestamp has no such attribute at all rather
        than a None one, and the attribute access itself used to raise into
        `_on_eventsub_message`'s handler.
        """
        event = make_eventsub_event()
        del type(event.metadata).message_timestamp

        payload = map_chat_message(event)

        assert payload["sent_at"] is None
        assert payload["text"] == "hello world"
        assert payload["broadcaster_id"] == 123

    def test_an_unreadable_timestamp_publishes_a_null_not_an_exception(self):
        assert to_epoch_ms("not-a-timestamp") is None
        assert to_epoch_ms(1787918400) is None, "an int is not the envelope shape"
        assert to_epoch_ms("") is None
        payload = map_chat_message(make_eventsub_event(sent_at="not-a-timestamp"))
        assert payload["sent_at"] is None

    def test_badges_become_a_dict_and_drive_the_two_booleans(self):
        payload = map_chat_message(
            make_eventsub_event(badges=[("subscriber", "12"), ("moderator", "1")])
        )
        assert payload["metadata"]["badges"] == {"subscriber": "12", "moderator": "1"}
        assert payload["metadata"]["is_subscriber"] is True
        assert payload["metadata"]["is_mod"] is True

    def test_no_badges_means_neither_flag(self):
        payload = map_chat_message(make_eventsub_event(badges=[]))
        assert payload["metadata"]["badges"] == {}
        assert payload["metadata"]["is_subscriber"] is False
        assert payload["metadata"]["is_mod"] is False

    def test_emotes_stays_empty(self):
        """IRC never populated it. Starting now would change the payload in a
        feature that promises not to."""
        payload = map_chat_message(
            make_eventsub_event(badges=[("subscriber", "1")])
        )
        assert payload["metadata"]["emotes"] == {}

    def test_broadcaster_id_comes_from_the_event(self):
        """No login-to-id lookup, so no message is dropped for a missing map.

        The IRC handler returned early whenever `broadcaster_ids` had no entry
        for the room, which silently lost every message from a channel joined
        before the poll that named it.
        """
        payload = map_chat_message(make_eventsub_event(broadcaster_id=147))
        assert payload["broadcaster_id"] == 147
        assert isinstance(payload["broadcaster_id"], int)

    def test_an_anonymous_chatter_maps_to_user_id_zero(self):
        payload = map_chat_message(make_eventsub_event(chatter_id=""))
        assert payload["user_id"] == 0

    def test_message_id_falls_back_to_a_generated_uuid(self):
        payload = map_chat_message(make_eventsub_event(message_id=None))
        assert payload["message_id"]
        assert isinstance(payload["message_id"], str)

    def test_the_service_publishes_what_the_mapper_produced(self):
        """T022 -- the handler the pool calls reaches the existing producer."""

        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            published = []
            service._publish_chat_message = lambda bid, msg: published.append((bid, msg))
            await service._on_eventsub_message(make_eventsub_event(broadcaster_id=99))
            assert published[0][0] == 99
            assert published[0][1]["broadcaster_id"] == 99

        asyncio.run(run())

    def test_a_broken_event_does_not_kill_the_socket(self):
        """One malformed event must not stop delivery for the other 299."""

        async def run():
            service = StreamMonitoringService()
            service._publish_chat_message = MagicMock()
            await service._on_eventsub_message(object())  # no .event at all
            service._publish_chat_message.assert_not_called()

        asyncio.run(run())


def make_eventsub_event(
    broadcaster_id=123,
    chatter_id="456",
    text="hello world",
    badges=(),
    message_id="msg-uuid",
    sent_at=None,
):
    """A stand-in for ChannelChatMessageEvent, shaped like the real one.

    pyTwitchAPI has already turned `metadata.message_timestamp` into a
    tz-aware datetime by the time an event reaches a callback, so the fake
    carries a datetime too.
    """
    badge_objects = [
        type("Badge", (), {"set_id": set_id, "id": badge_id})() for set_id, badge_id in badges
    ]
    return type(
        "Event",
        (),
        {
            "metadata": type(
                "Meta",
                (),
                {
                    "message_timestamp": sent_at
                    or datetime(2026, 8, 28, 12, 0, 0, 250000, tzinfo=timezone.utc)
                },
            )(),
            "event": type(
                "Data",
                (),
                {
                    "broadcaster_user_id": str(broadcaster_id),
                    "chatter_user_id": chatter_id,
                    "chatter_user_login": "a_viewer",
                    "message_id": message_id,
                    "message": type("Message", (), {"text": text})(),
                    "badges": badge_objects,
                },
            )(),
        },
    )()


_ABSENT = object()
SUPPRESSION_OCCURRED_AT = datetime(
    2026, 9, 4, 12, 0, 0, 123000, tzinfo=timezone.utc
)
SUPPRESSION_RECEIVED_AT_MS = 1788523200456
KNOWN_IGNORED_NOTICE_TYPES = (
    "sub",
    "resub",
    "gift_paid_upgrade",
    "prime_paid_upgrade",
    "unraid",
    "pay_it_forward",
    "announcement",
    "bits_badge_tier",
    "charity_donation",
)


def make_suppression_event(
    *,
    notice_type="community_sub_gift",
    broadcaster_id="123",
    occurred_at=SUPPRESSION_OCCURRED_AT,
    notice_id="notice-uuid",
    viewer_count=321,
):
    """A ChannelChatNotificationEvent fake that can omit TwitchObject fields."""
    metadata = {}
    if occurred_at is not _ABSENT:
        metadata["message_timestamp"] = occurred_at
    if notice_id is not _ABSENT:
        metadata["message_id"] = notice_id

    data = {
        "system_message": "DO_NOT_LOG_SYSTEM_MESSAGE",
        "message": "DO_NOT_LOG_CHAT_TEXT",
        "chatter_user_id": "DO_NOT_LOG_USER_ID",
    }
    if notice_type is not _ABSENT:
        data["notice_type"] = notice_type
    if broadcaster_id is not _ABSENT:
        data["broadcaster_user_id"] = broadcaster_id
    if notice_type == "raid" and viewer_count is not _ABSENT:
        data["raid"] = type(
            "RaidNotice", (), {"viewer_count": viewer_count}
        )()
    elif notice_type == "community_sub_gift":
        data["community_sub_gift"] = type(
            "CommunityGiftNotice",
            (),
            {"total": 50, "gifter_user_name": "DO_NOT_LOG_GIFTER"},
        )()
    elif notice_type == "sub_gift":
        data["sub_gift"] = type(
            "GiftNotice",
            (),
            {"sub_tier": "3000", "recipient_user_name": "DO_NOT_LOG_RECIPIENT"},
        )()

    return type(
        "NotificationEvent",
        (),
        {
            "metadata": type("NotificationMetadata", (), metadata)(),
            "event": type("NotificationData", (), data)(),
        },
    )()


def expected_suppression_payload(
    notice_type,
    *,
    broadcaster_id=123,
    notice_id="notice-uuid",
    viewer_count=None,
):
    return {
        "schema_version": 1,
        "broadcaster_id": broadcaster_id,
        "notice_type": notice_type,
        "occurred_at_ms": to_epoch_ms(SUPPRESSION_OCCURRED_AT),
        "notice_id": notice_id,
        "received_at_ms": SUPPRESSION_RECEIVED_AT_MS,
        "viewer_count": viewer_count,
    }


def service_counter_value(metric_name, **labels):
    metric = getattr(stream_monitoring_service, metric_name)
    return metric.labels(**labels)._value.get()


class TestSuppressionEventMapping:
    """T004 -- pure version-1 producer contract."""

    @staticmethod
    def map(event):
        mapper = getattr(eventsub_pool, "map_suppression_event")
        result = mapper(
            event,
            received_at_ms=SUPPRESSION_RECEIVED_AT_MS,
        )
        result_type = getattr(eventsub_pool, "SuppressionMapResult")
        assert isinstance(result, result_type)
        return result

    def test_schema_version_constant_is_one(self):
        assert getattr(eventsub_pool, "SUPPRESSION_SCHEMA_VERSION") == 1

    @pytest.mark.parametrize(
        ("notice_type", "viewer_count"),
        [
            ("community_sub_gift", None),
            ("sub_gift", None),
            ("raid", 321),
        ],
    )
    def test_trigger_maps_to_exact_version_one_record(
        self, notice_type, viewer_count
    ):
        result = self.map(
            make_suppression_event(
                notice_type=notice_type,
                viewer_count=viewer_count,
            )
        )

        assert result.kind == "mapped"
        assert result.notice_type == notice_type
        assert result.reason is None
        assert result.payload == expected_suppression_payload(
            notice_type,
            viewer_count=viewer_count,
        )
        assert isinstance(result.payload["schema_version"], int)
        assert isinstance(result.payload["broadcaster_id"], int)
        assert isinstance(result.payload["notice_type"], str)
        assert isinstance(result.payload["occurred_at_ms"], int)
        assert isinstance(result.payload["received_at_ms"], int)
        assert result.payload["notice_id"] is None or isinstance(
            result.payload["notice_id"], str
        )
        assert result.payload["viewer_count"] is None or isinstance(
            result.payload["viewer_count"], int
        )
        assert set(result.payload) == {
            "schema_version",
            "broadcaster_id",
            "notice_type",
            "occurred_at_ms",
            "notice_id",
            "received_at_ms",
            "viewer_count",
        }
        assert not {
            "suppress_until_ms",
            "deadline_ms",
            "window_ms",
            "window_seconds",
            "system_message",
            "message",
            "text",
            "chatter_user_id",
            "user_id",
        } & set(result.payload)

    def test_optional_fields_are_null_when_twitch_omits_them(self):
        event = make_suppression_event(
            notice_type="raid",
            notice_id=_ABSENT,
            viewer_count=_ABSENT,
        )

        result = self.map(event)

        assert result.payload == expected_suppression_payload(
            "raid",
            notice_id=None,
            viewer_count=None,
        )

    def test_raid_viewer_count_is_diagnostic_only(self):
        small = self.map(
            make_suppression_event(notice_type="raid", viewer_count=1)
        ).payload
        large = self.map(
            make_suppression_event(notice_type="raid", viewer_count=100_000)
        ).payload

        assert {
            key: value for key, value in small.items() if key != "viewer_count"
        } == {
            key: value for key, value in large.items() if key != "viewer_count"
        }
        assert small["viewer_count"] == 1
        assert large["viewer_count"] == 100_000
        assert not {
            "suppress_until_ms",
            "deadline_ms",
            "window_ms",
            "window_seconds",
        } & set(small)

    @pytest.mark.parametrize(
        ("notice_type", "metric_label"),
        [
            *(
                (notice_type, notice_type)
                for notice_type in KNOWN_IGNORED_NOTICE_TYPES
            ),
            ("future_notice_type", "other"),
            (_ABSENT, "other"),
        ],
    )
    def test_non_trigger_is_a_typed_ignored_result(
        self, notice_type, metric_label
    ):
        result = self.map(
            make_suppression_event(notice_type=notice_type)
        )

        assert result.kind == "ignored"
        assert result.payload is None
        assert result.notice_type == metric_label
        assert result.reason is None

    @pytest.mark.parametrize(
        "broadcaster_id",
        [None, "", "not-an-integer", _ABSENT],
    )
    def test_untrustworthy_identity_is_malformed(self, broadcaster_id):
        result = self.map(
            make_suppression_event(broadcaster_id=broadcaster_id)
        )

        assert result.kind == "malformed"
        assert result.payload is None
        assert result.notice_type == "community_sub_gift"
        assert result.reason == "identity"

    @pytest.mark.parametrize(
        "occurred_at",
        [None, "", "not-a-timestamp", _ABSENT],
    )
    def test_untrustworthy_occurrence_time_is_not_replaced_by_ingest_time(
        self, occurred_at
    ):
        result = self.map(
            make_suppression_event(occurred_at=occurred_at)
        )

        assert result.kind == "malformed"
        assert result.payload is None
        assert result.notice_type == "community_sub_gift"
        assert result.reason == "occurred_at"

    def test_ignored_classification_precedes_unrelated_missing_fields(self):
        event = make_suppression_event(
            notice_type=_ABSENT,
            broadcaster_id=_ABSENT,
            occurred_at=_ABSENT,
        )

        result = self.map(event)

        assert result.kind == "ignored"
        assert result.notice_type == "other"
        assert result.reason is None


class TestSuppressionNotificationPublisher:
    """T033 -- callback wiring, publication, and bounded observability."""

    def test_transport_receives_the_notification_handler(self):
        async def run():
            service = StreamMonitoringService()
            service.twitch = object()
            pool = MagicMock()
            pool.start = AsyncMock()

            with patch.object(
                stream_monitoring_service,
                "EventSubPoolTransport",
                return_value=pool,
            ) as pool_type:
                assert await service._build_transport() is pool

            args, kwargs = pool_type.call_args
            assert args == (service.twitch, service._on_eventsub_message)
            assert (
                kwargs["notification_handler"]
                == service._on_eventsub_notification
            )
            assert kwargs["on_subscriptions_lost"] == service._on_subscriptions_lost
            pool.start.assert_awaited_once_with()

        asyncio.run(run())

    @pytest.mark.parametrize(
        ("notice_type", "viewer_count"),
        [
            ("community_sub_gift", None),
            ("sub_gift", None),
            ("raid", 321),
        ],
    )
    def test_valid_notice_publishes_exact_key_and_json(
        self, notice_type, viewer_count
    ):
        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            before = service_counter_value(
                "kafka_messages_produced",
                topic="suppression-events",
            )

            await service._on_eventsub_notification(
                make_suppression_event(
                    notice_type=notice_type,
                    viewer_count=viewer_count,
                ),
                received_at_ms=SUPPRESSION_RECEIVED_AT_MS,
            )

            service.kafka_producer.produce.assert_called_once()
            produced = service.kafka_producer.produce.call_args.kwargs
            expected = expected_suppression_payload(
                notice_type,
                viewer_count=viewer_count,
            )
            assert set(produced) == {"topic", "key", "value", "callback"}
            assert produced["topic"] == "suppression-events"
            assert produced["key"] == b"123"
            assert produced["value"] == json.dumps(expected).encode("utf-8")
            decoded = json.loads(produced["value"].decode("utf-8"))
            assert produced["key"] == str(
                decoded["broadcaster_id"]
            ).encode("utf-8")
            assert produced["callback"] == service._delivery_callback
            service.kafka_producer.poll.assert_called_once_with(0)
            assert [
                call[0] for call in service.kafka_producer.method_calls
            ] == ["produce", "poll"]
            assert (
                service_counter_value(
                    "kafka_messages_produced",
                    topic="suppression-events",
                )
                - before
                == 1
            )

        asyncio.run(run())

    @pytest.mark.parametrize(
        ("notice_type", "metric_label"),
        [
            *(
                (notice_type, notice_type)
                for notice_type in KNOWN_IGNORED_NOTICE_TYPES
            ),
            ("future_notice_type", "other"),
            (_ABSENT, "other"),
        ],
    )
    def test_ignored_notice_is_counted_with_a_bounded_label(
        self, notice_type, metric_label
    ):
        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            metric = getattr(
                stream_monitoring_service,
                "suppression_notices_ignored_total",
            )
            assert tuple(metric._labelnames) == ("notice_type",)
            before = service_counter_value(
                "suppression_notices_ignored_total",
                notice_type=metric_label,
            )

            await service._on_eventsub_notification(
                make_suppression_event(notice_type=notice_type),
                received_at_ms=SUPPRESSION_RECEIVED_AT_MS,
            )

            service.kafka_producer.produce.assert_not_called()
            assert (
                service_counter_value(
                    "suppression_notices_ignored_total",
                    notice_type=metric_label,
                )
                - before
                == 1
            )
            assert metric_label in {
                *KNOWN_IGNORED_NOTICE_TYPES,
                "other",
            }

        asyncio.run(run())

    @pytest.mark.parametrize(
        ("reason", "event"),
        [
            (
                "identity",
                make_suppression_event(
                    notice_type="raid",
                    broadcaster_id=_ABSENT,
                ),
            ),
            (
                "occurred_at",
                make_suppression_event(
                    notice_type="raid",
                    occurred_at=_ABSENT,
                ),
            ),
        ],
    )
    def test_malformed_notice_is_counted_and_structured_logged(
        self, reason, event, caplog
    ):
        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            metric = getattr(
                stream_monitoring_service,
                "suppression_notices_malformed_total",
            )
            assert tuple(metric._labelnames) == ("reason",)
            before = service_counter_value(
                "suppression_notices_malformed_total",
                reason=reason,
            )

            with caplog.at_level(logging.WARNING):
                await service._on_eventsub_notification(
                    event,
                    received_at_ms=SUPPRESSION_RECEIVED_AT_MS,
                )

            service.kafka_producer.produce.assert_not_called()
            assert (
                service_counter_value(
                    "suppression_notices_malformed_total",
                    reason=reason,
                )
                - before
                == 1
            )
            records = [
                record
                for record in caplog.records
                if getattr(record, "reason", None) == reason
            ]
            assert len(records) == 1
            assert records[0].levelno >= logging.WARNING
            assert records[0].notice_type == "raid"
            assert records[0].notice_id == "notice-uuid"
            assert "DO_NOT_LOG_SYSTEM_MESSAGE" not in caplog.text
            assert "DO_NOT_LOG_CHAT_TEXT" not in caplog.text
            assert "DO_NOT_LOG_USER_ID" not in caplog.text

        asyncio.run(run())

    def test_produce_exception_is_contained_and_chat_handler_still_runs(
        self, caplog
    ):
        async def run():
            service = StreamMonitoringService()
            service.kafka_producer = MagicMock()
            service.kafka_producer.produce.side_effect = RuntimeError(
                "broker unavailable"
            )
            before = service_counter_value(
                "kafka_messages_produced",
                topic="suppression-events",
            )

            with caplog.at_level(logging.ERROR):
                await service._on_eventsub_notification(
                    make_suppression_event(),
                    received_at_ms=SUPPRESSION_RECEIVED_AT_MS,
                )

            service.kafka_producer.poll.assert_not_called()
            assert (
                service_counter_value(
                    "kafka_messages_produced",
                    topic="suppression-events",
                )
                == before
            )
            assert any(
                record.message == "Failed to publish suppression event"
                for record in caplog.records
            )

            service._publish_chat_message = MagicMock()
            await service._on_eventsub_message(make_eventsub_event())
            service._publish_chat_message.assert_called_once()

        asyncio.run(run())


class TestSuppressionTopicConfiguration:
    """T034 -- checked-in topic shape and producer topic identity."""

    def test_kafka_init_adds_only_the_expected_suppression_topic(self):
        repository_root = Path(__file__).resolve().parents[2]
        compose = (repository_root / "docker-compose.yml").read_text()
        kafka_init = compose.split("\n  kafka-init:", 1)[1]
        kafka_init = kafka_init.split("\n  flink-jobmanager:", 1)[0]

        suppression_command = (
            "kafka-topics --bootstrap-server kafka:29092 --create "
            "--if-not-exists --topic suppression-events --partitions 4 "
            "--replication-factor 1 --config retention.ms=3600000"
        )
        chat_command = (
            "kafka-topics --bootstrap-server kafka:29092 --create "
            "--if-not-exists --topic chat-messages --partitions 4 "
            "--replication-factor 1 --config retention.ms=3600000"
        )
        lifecycle_command = (
            "kafka-topics --bootstrap-server kafka:29092 --create "
            "--if-not-exists --topic stream-lifecycle --partitions 10 "
            "--replication-factor 1 --config retention.ms=604800000"
        )

        assert kafka_init.count(suppression_command) == 1
        assert kafka_init.count(chat_command) == 1
        assert kafka_init.count(lifecycle_command) == 1
        assert kafka_init.count("--topic suppression-events") == 1
        assert kafka_init.count("--topic chat-messages") == 1
        assert kafka_init.count("--topic stream-lifecycle") == 1

    def test_producer_topic_constant_is_exact(self):
        assert (
            getattr(stream_monitoring_service, "SUPPRESSION_EVENTS_TOPIC")
            == "suppression-events"
        )


class FakeRefusalStore(RefusalStore):
    """An in-memory `streamers.eventsub_refused_at`."""

    def __init__(self, marks=None):
        # broadcaster id -> stale?
        self.marks = dict(marks or {})
        self.marked = []
        self.cleared = []

    def refusals(self, broadcaster_ids):
        return {bid: stale for bid, stale in self.marks.items() if bid in set(broadcaster_ids)}

    def mark_refused(self, broadcaster_id):
        self.marked.append(broadcaster_id)
        self.marks[broadcaster_id] = False  # a fresh mark is never stale

    def clear_refusal(self, broadcaster_id):
        self.cleared.append(broadcaster_id)
        self.marks.pop(broadcaster_id, None)


class TestRefusalCache:
    """T025c / FR-007, D5 -- refusals persist, and they expire."""

    def test_a_recent_refusal_is_skipped(self):
        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            store = FakeRefusalStore({2: False})
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2), ("c", 3)])

            await reconciler.reconcile_once()

            assert transport.create_calls == [1, 3]
            assert reconciler.subscription_count == 2

        asyncio.run(run())

    def test_a_stale_refusal_is_retried_once_and_cleared_on_success(self):
        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            store = FakeRefusalStore({2: True})
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2)])

            await reconciler.reconcile_once()

            assert sorted(transport.create_calls) == [1, 2]
            assert store.cleared == [2]
            assert 2 not in store.marks

        asyncio.run(run())

    def test_a_refusal_is_recorded_so_the_next_pass_skips_it(self):
        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport(refuse={2})
            store = FakeRefusalStore()
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2)])

            await reconciler.reconcile_once()
            assert store.marked == [2]

            transport.create_calls.clear()
            await reconciler.reconcile_once()
            assert transport.create_calls == []  # 1 is held, 2 is now skipped

        asyncio.run(run())

    def test_a_fresh_refusal_resets_a_stale_mark(self):
        """The retry that fails restarts the 7 days rather than retrying forever."""

        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport(refuse={2})
            store = FakeRefusalStore({2: True})
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("b", 2)])

            await reconciler.reconcile_once()

            assert transport.create_calls == [2]  # the stale mark bought a retry
            assert store.marked == [2]            # which refused, so the mark is reset
            assert store.cleared == []
            assert store.marks[2] is False

        asyncio.run(run())

    def test_a_database_fault_does_not_stop_the_reconciler(self):
        """A store that throws must not leave every channel unsubscribed.

        The reconciler runs as one long-lived task; an exception escaping a
        pass ends the loop and the service goes quiet with no subscriptions
        and no error after the first line. Falling back to "attempt
        everything" costs at most one wasted POST per refused channel.
        """

        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            store = FakeRefusalStore({2: False})
            store.refusals = MagicMock(side_effect=ConnectionError("postgres is away"))
            reconciler = make_reconciler(transport, fake_redis)
            reconciler.refusal_store = store
            seed_desired(fake_redis, [("a", 1), ("b", 2)])

            await reconciler.reconcile_once()

            assert sorted(transport.create_calls) == [1, 2]
            assert reconciler.subscription_count == 2

        asyncio.run(run())

    def test_without_a_store_every_channel_is_attempted(self):
        """Phase 1 behaviour is preserved when no store is supplied."""

        async def run():
            fake_redis = FakeRedis()
            transport = StubTransport()
            reconciler = make_reconciler(transport, fake_redis)
            seed_desired(fake_redis, [("a", 1), ("b", 2)])
            await reconciler.reconcile_once()
            assert sorted(transport.create_calls) == [1, 2]

        asyncio.run(run())


# ---------------------------------------------------------------------------
# The 7-day self-heal, against a real Postgres
# ---------------------------------------------------------------------------
#
# `make_interval`, `ANY(%s)` and the NULL handling are SQL, and a hand-written
# fake cursor can only confirm that the string was sent -- not that Postgres
# agrees with what it means. Phase 1 checked the Redis seam the same way, for
# the same reason.
#
# The fixture builds its own schema and drops it afterwards, and refuses to run
# unless `streamers` really resolves inside that schema, so it can never touch
# the deployed table. It skips when there is no database to talk to.

TEST_SCHEMA = "spec004_selfheal_test"
# Deliberately localhost, not the deployed host. The fixture runs DDL --
# CREATE SCHEMA and DROP SCHEMA CASCADE -- and defaulting that at the live
# database would put every `pytest` run on production, and put its credential
# in this file. `docker compose --profile local-db up postgres` gives a local
# one; set TEST_POSTGRES_URL to point somewhere else on purpose.
TEST_POSTGRES_URL = os.getenv(
    "TEST_POSTGRES_URL", "postgresql://twitch:twitch_password@localhost:5432/twitch"
)


class SingleConnectionPool:
    """The two-method slice of psycopg2's pool that this code uses."""

    def __init__(self, conn):
        self.conn = conn

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        pass


@pytest.fixture
def streamers_table():
    psycopg2 = pytest.importorskip("psycopg2")
    try:
        conn = psycopg2.connect(TEST_POSTGRES_URL, connect_timeout=3)
    except Exception as e:  # pragma: no cover -- environment, not logic
        pytest.skip(f"no Postgres available for the self-heal check: {e}")

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {TEST_SCHEMA}")
        cur.execute(f"SET search_path TO {TEST_SCHEMA}")
        cur.execute(
            """
            CREATE TABLE streamers (
                streamer_id BIGINT PRIMARY KEY,
                streamer_login VARCHAR(255) NOT NULL,
                allows_clipping BOOLEAN DEFAULT TRUE,
                first_seen_at TIMESTAMPTZ DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ DEFAULT NOW(),
                eventsub_refused_at TIMESTAMPTZ,
                clipping_disabled_at TIMESTAMPTZ
            )
            """
        )
        # Refuse to go anywhere near the deployed table.
        cur.execute(
            "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = to_regclass('streamers')"
        )
        resolved = cur.fetchone()
        assert resolved and resolved[0] == TEST_SCHEMA, (
            f"'streamers' resolves to {resolved} rather than the test schema; refusing to run"
        )
    conn.commit()

    try:
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        conn.commit()
        conn.close()


def add_streamer(conn, streamer_id, *, allows_clipping=True, refused_days_ago=None,
                 disabled_days_ago=None, disabled_at_null=False):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO streamers (streamer_id, streamer_login, allows_clipping, "
            "eventsub_refused_at, clipping_disabled_at) VALUES (%s, %s, %s, "
            "CASE WHEN %s IS NULL THEN NULL ELSE NOW() - make_interval(days => %s) END, "
            "CASE WHEN %s THEN NULL WHEN %s IS NULL THEN NULL "
            "     ELSE NOW() - make_interval(days => %s) END)",
            (
                streamer_id, f"login{streamer_id}", allows_clipping,
                refused_days_ago, refused_days_ago or 0,
                disabled_at_null, disabled_days_ago, disabled_days_ago or 0,
            ),
        )
    conn.commit()


@pytest.mark.skipif(
    "TEST_POSTGRES_URL" not in os.environ,
    reason="set TEST_POSTGRES_URL explicitly for isolated feature 006 evidence",
)
class TestStreamerMetadataBatchAgainstPostgres:
    def test_900_inputs_use_one_statement_and_preserve_non_batch_columns(
        self, streamers_table
    ):
        with streamers_table.cursor() as cursor:
            cursor.execute("TRUNCATE streamers")
            cursor.execute(
                """
                INSERT INTO streamers (
                    streamer_id,
                    streamer_login,
                    allows_clipping,
                    first_seen_at,
                    last_seen_at,
                    eventsub_refused_at,
                    clipping_disabled_at
                )
                VALUES (
                    1,
                    'old-login',
                    FALSE,
                    NOW() - INTERVAL '30 days',
                    NOW() - INTERVAL '1 day',
                    NOW() - INTERVAL '2 days',
                    NOW() - INTERVAL '3 days'
                )
                RETURNING first_seen_at, last_seen_at, eventsub_refused_at,
                          clipping_disabled_at
                """
            )
            before = cursor.fetchone()
        streamers_table.commit()

        counted = ProductionCallCountingConnection(streamers_table)
        pool = CountingPool(counted)
        service = StreamMonitoringService()
        service.db_pool = pool
        records = [
            (streamer_id, f"login-{streamer_id}")
            for streamer_id in range(1, 900)
        ]
        records.append((7, "final-seven"))
        pool.reset_measured()

        assert service._upsert_streamer_batch(records) is True

        assert counted.execute_count == 1
        assert counted.commit_count == 1
        assert counted.rollback_count == 0
        with streamers_table.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*), COUNT(DISTINCT last_seen_at) FROM streamers"
            )
            assert cursor.fetchone() == (899, 1)
            cursor.execute(
                """
                SELECT streamer_login, first_seen_at, last_seen_at,
                       allows_clipping, eventsub_refused_at,
                       clipping_disabled_at
                FROM streamers
                WHERE streamer_id = 1
                """
            )
            row_one = cursor.fetchone()
            cursor.execute(
                "SELECT streamer_login FROM streamers WHERE streamer_id = 7"
            )
            row_seven = cursor.fetchone()

        assert row_one[0] == "login-1"
        assert row_one[1] == before[0]
        assert row_one[2] > before[1]
        assert row_one[3] is False
        assert row_one[4] == before[2]
        assert row_one[5] == before[3]
        assert row_seven == ("final-seven",)

    def test_poison_batch_rolls_back_and_connection_is_reusable(
        self, streamers_table
    ):
        with streamers_table.cursor() as cursor:
            cursor.execute("TRUNCATE streamers")
        streamers_table.commit()

        counted = ProductionCallCountingConnection(streamers_table)
        pool = CountingPool(counted)
        service = StreamMonitoringService()
        service.db_pool = pool
        pool.reset_measured()

        assert service._upsert_streamer_batch(
            [(1, "valid"), (2, "x" * 256)]
        ) is False

        assert counted.execute_count == 1
        assert counted.commit_count == 0
        assert counted.rollback_count == 1
        assert pool.discard_count == 0
        with streamers_table.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM streamers")
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT 1")
            assert cursor.fetchone() == (1,)

    def test_next_poll_reconstructs_and_retries_the_whole_batch(
        self, streamers_table
    ):
        with streamers_table.cursor() as cursor:
            cursor.execute("TRUNCATE streamers")
        streamers_table.commit()

        counted = ProductionCallCountingConnection(streamers_table)
        pool = CountingPool(counted)
        fake_redis = FakeRedis()
        service = make_poller(
            [],
            fake_redis,
            ranked_records=[("valid", 1), ("x" * 256, 2)],
            pool=pool,
        )

        with patch.object(stream_monitoring_service, "JOIN_THRESHOLD", 2), \
             patch.object(stream_monitoring_service, "LEAVE_THRESHOLD", 2):
            asyncio.run(service.poll_top_streams())
            assert service.test_side_effects.final_outcomes == [
                "metadata_failed"
            ]
            with streamers_table.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM streamers")
                assert cursor.fetchone() == (0,)

            service.twitch = FakeTwitch(
                [make_stream("valid", 1), make_stream("corrected", 2)]
            )
            asyncio.run(service.poll_top_streams())

        with streamers_table.cursor() as cursor:
            cursor.execute(
                "SELECT streamer_id, streamer_login FROM streamers "
                "ORDER BY streamer_id"
            )
            assert cursor.fetchall() == [(1, "valid"), (2, "corrected")]
        assert service.test_side_effects.final_outcomes[-1] == "success"


class TestRefusalStoreAgainstPostgres:
    """T025c / FR-007 -- the SQL behind the 7-day refusal re-check."""

    def test_a_fresh_mark_stands_and_an_old_one_is_stale(self, streamers_table):
        add_streamer(streamers_table, 1)  # never refused
        add_streamer(streamers_table, 2, refused_days_ago=1)
        add_streamer(streamers_table, 3, refused_days_ago=REFUSAL_RECHECK_DAYS + 1)

        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        marks = store.refusals([1, 2, 3])

        assert 1 not in marks           # no mark at all -- attempt it
        assert marks[2] is False        # mark stands -- skip it
        assert marks[3] is True         # stale -- one retry

    def test_the_boundary_sits_at_seven_days(self, streamers_table):
        """An hour short of the interval still stands; an hour past is stale.

        Pins the interval itself, not just that some interval exists. Exactly
        at the boundary is not testable against a live clock: `NOW()` moves
        between the insert and the read, so the row is always a few
        milliseconds older than the offset it was written with.
        """
        with streamers_table.cursor() as cur:
            cur.execute(
                "INSERT INTO streamers (streamer_id, streamer_login, eventsub_refused_at) "
                "VALUES (1, 'just_inside',  NOW() - make_interval(hours => %s)), "
                "       (2, 'just_outside', NOW() - make_interval(hours => %s))",
                (REFUSAL_RECHECK_DAYS * 24 - 1, REFUSAL_RECHECK_DAYS * 24 + 1),
            )
        streamers_table.commit()

        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        marks = store.refusals([1, 2])
        assert marks[1] is False
        assert marks[2] is True

    def test_mark_and_clear_round_trip(self, streamers_table):
        add_streamer(streamers_table, 1)
        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))

        store.mark_refused(1)
        assert store.refusals([1]) == {1: False}

        store.clear_refusal(1)
        assert store.refusals([1]) == {}

    def test_a_fresh_refusal_resets_a_stale_timestamp(self, streamers_table):
        """The retry that refuses again restarts the seven days."""
        add_streamer(streamers_table, 1, refused_days_ago=REFUSAL_RECHECK_DAYS + 5)
        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        assert store.refusals([1])[1] is True

        store.mark_refused(1)
        assert store.refusals([1])[1] is False

    def test_an_unknown_id_is_simply_absent(self, streamers_table):
        store = PostgresRefusalStore(SingleConnectionPool(streamers_table))
        assert store.refusals([999]) == {}
        assert store.refusals([]) == {}


class TestClippingRecheckAgainstPostgres:
    """T025c / FR-013 -- a stale `allows_clipping = FALSE` re-enters ranking."""

    def test_stale_disabled_streamers_re_enter_the_ranking(self, streamers_table):
        add_streamer(streamers_table, 1)                                   # allowed
        add_streamer(streamers_table, 2, allows_clipping=False, disabled_days_ago=1)
        add_streamer(
            streamers_table, 3, allows_clipping=False,
            disabled_days_ago=stream_monitoring_service.CLIPPING_RECHECK_DAYS + 1,
        )

        service = StreamMonitoringService()
        service.db_pool = SingleConnectionPool(streamers_table)

        # 3 is stale, so it is NOT in the disabled set and is ranked again.
        assert service._get_clipping_disabled_ids([1, 2, 3]) == {2}

    def test_a_row_with_no_timestamp_stays_disabled(self, streamers_table):
        """A FALSE flag with no timestamp predates the migration backfill.

        No timestamp is no evidence the mark is stale, so it keeps the skip
        rather than handing every legacy row a retry at once.
        """
        add_streamer(streamers_table, 1, allows_clipping=False, disabled_at_null=True)
        service = StreamMonitoringService()
        service.db_pool = SingleConnectionPool(streamers_table)
        assert service._get_clipping_disabled_ids([1]) == {1}

    def test_no_ids_costs_no_query(self, streamers_table):
        service = StreamMonitoringService()
        service.db_pool = SingleConnectionPool(streamers_table)
        assert service._get_clipping_disabled_ids([]) == set()


class TestScopeGuard:
    """T017 -- a token without `user:read:chat` must not poison the cache."""

    def test_the_refusal_cache_is_off_without_the_chat_scope(self):
        """Every channel refuses for one reason that is not the broadcasters'.

        Persisting those refusals would mark the whole monitored set and skip
        it for seven days, turning a token mistake into a week-long outage.
        """

        service = StreamMonitoringService()
        service.db_pool = MagicMock()

        service.has_chat_scope = False
        assert service._build_refusal_store() is None

        service.has_chat_scope = True
        assert isinstance(service._build_refusal_store(), PostgresRefusalStore)

    def test_the_scope_map_covers_every_seeded_scope(self):
        """A scope in the seed script but not in the map is silently dropped,
        and the feature that needs it fails at run time instead of at start."""
        seeded = {"user:read:chat", "clips:edit"}
        assert seeded <= set(stream_monitoring_service.SCOPE_MAP)
