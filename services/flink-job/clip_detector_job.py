#!/usr/bin/env python3
"""
PyFlink Clip Detector Job

Consumes chat messages from Kafka, detects anomalies using sliding windows,
creates clips via Twitch API, and stores metadata in Postgres.
"""

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

import requests
from prometheus_client import Counter, Gauge, start_http_server
from pyflink.common import Row, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.time import Duration, Time
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import (
    KeyedProcessFunction,
    OutputTag,
    ProcessFunction,
    StreamExecutionEnvironment,
)
from pyflink.datastream.connectors.kafka import (
    KafkaOffsetsInitializer,
    KafkaSource,
)
from pyflink.datastream.state import (
    MapStateDescriptor,
    StateTtlConfig,
    ValueStateDescriptor,
)

from clip_attempt import ClipAttempt, ClipPolicy, RealClock
from spike_detector import (
    DetectorConfig,
    HoldState,
    WATERMARK_OUT_OF_ORDERNESS_SECONDS,
    evaluate,
    is_command,
)
from token_manager import TwitchCredentials

# Configuration
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = os.getenv("POSTGRES_PORT", "5432")
POSTGRES_DB = os.getenv("POSTGRES_DB", "twitch")
POSTGRES_USER = os.getenv("POSTGRES_USER", "twitch")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "twitch_password")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TWITCH_TOKEN_FILE = os.getenv("TWITCH_TOKEN_FILE", "/opt/flink/secrets/twitch_user_tokens.json")
FLINK_PARALLELISM = int(os.getenv("FLINK_PARALLELISM", "4"))

# HTTP status codes that are retryable (transient errors)
RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("clip_detector")

# Prometheus metrics - lazily initialized to avoid pickling issues
METRICS_PORT = int(os.getenv("METRICS_PORT", "9250"))

# Global metrics registry (initialized lazily in TaskManager)
# Note: We can't use a threading.Lock here because it can't be pickled by Flink
_metrics_initialized = False
_anomalies_detected_total = None
_clips_created_success_total = None
_clips_created_failed_total = None
_clip_creation_duration_seconds = None


def _init_metrics(subtask_index: int = 0):
    """Initialize Prometheus metrics (called once per parallel subtask process).

    Each parallel subtask runs in its own Python worker process, so each one
    needs its own HTTP server. They can't all bind METRICS_PORT: only the
    first subtask to call start_http_server() would win that race, and the
    other subtasks' counters -- covering whichever broadcasters keyBy hashed
    onto them -- would silently never be scraped. Binding METRICS_PORT +
    subtask_index gives every subtask its own port instead.
    """
    global _metrics_initialized, _anomalies_detected_total, _clips_created_success_total
    global _clips_created_failed_total, _clip_creation_duration_seconds

    if _metrics_initialized:
        return

    from prometheus_client import REGISTRY

    # Helper to get or create a metric
    def get_or_create_counter(name, desc, labels):
        for collector in list(REGISTRY._names_to_collectors.values()):
            if hasattr(collector, '_name') and collector._name == name.replace('_total', ''):
                return collector
        return Counter(name, desc, labels)

    def get_or_create_gauge(name, desc, labels):
        for collector in list(REGISTRY._names_to_collectors.values()):
            if hasattr(collector, '_name') and collector._name == name:
                return collector
        return Gauge(name, desc, labels)

    try:
        _anomalies_detected_total = get_or_create_counter("anomalies_detected_total", "Total anomalies detected", ["broadcaster_id"])
        _clips_created_success_total = get_or_create_counter("clips_created_success_total", "Total clips created successfully", ["broadcaster_id"])
        _clips_created_failed_total = get_or_create_counter("clips_created_failed_total", "Total clip creation failures", ["broadcaster_id", "reason"])
        _clip_creation_duration_seconds = get_or_create_gauge("clip_creation_duration_seconds", "Time taken to create last clip", ["broadcaster_id"])

        # Start metrics server on a port unique to this subtask
        port = METRICS_PORT + subtask_index
        try:
            start_http_server(port)
            logger.info(f"Prometheus metrics server started on port {port} (subtask {subtask_index})")
        except OSError as e:
            if "Address already in use" in str(e):
                logger.warning(f"Metrics server already running on port {port} (subtask {subtask_index})")
            else:
                logger.warning(f"Metrics server error: {e}")

        logger.info("Prometheus metrics initialized")
    except Exception as e:
        logger.warning(f"Error initializing metrics: {e}")

    _metrics_initialized = True


@dataclass
class ChatMessage:
    """Represents a chat message."""
    broadcaster_id: int
    timestamp: int
    message_id: str
    text: str
    user_id: int
    user_name: str


@dataclass
class AnomalyEvent:
    """Represents a detected anomaly."""
    broadcaster_id: int
    detected_at: int
    message_count: int
    baseline_mean: float
    baseline_std: float


@dataclass
class ClipResult:
    """Represents a clip creation result."""
    broadcaster_id: int
    clip_id: str
    embed_url: str
    thumbnail_url: str
    detected_at: int
    success: bool
    intensity: Optional[float] = None  # Z-score: (message_count - mean) / std_dev
    duration: Optional[float] = None  # seconds, from Twitch Get Clips; may be null
    vod_offset: Optional[int] = None  # seconds into the VOD where the clip starts; may be null


class TwitchAPIError(Exception):
    """Custom exception for Twitch API errors."""
    def __init__(self, message: str, status_code: int, is_retryable: bool):
        super().__init__(message)
        self.status_code = status_code
        self.is_retryable = is_retryable


class TokenValidationError(Exception):
    """Raised when token validation fails at startup."""
    pass


class TwitchAPIClient:
    """Client for interacting with Twitch API using user OAuth tokens."""

    def __init__(self, client_id: str, client_secret: str, token_file: str, validate_on_init: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self._credentials = TwitchCredentials(Path(token_file))
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._load_tokens()
        if validate_on_init:
            self._validate_and_refresh_if_needed()

    def _mask_token(self, token: Optional[str]) -> str:
        """Mask a token for safe logging, showing only first 4 characters."""
        if not token:
            return "<empty>"
        if len(token) <= 4:
            return "****"
        return f"{token[:4]}...{len(token) - 4} more chars"

    def _load_tokens(self):
        """Load user tokens via the shared credentials module."""
        logger.info(f"Loading tokens from file: {self.token_file}")
        try:
            record = self._credentials.load()
        except FileNotFoundError as e:
            logger.error(f"TOKEN FILE NOT FOUND: {self.token_file}")
            logger.error("Please run seed_twitch_tokens.py to generate tokens first")
            raise TokenValidationError(str(e))
        except json.JSONDecodeError as e:
            logger.error(f"TOKEN FILE INVALID JSON: {self.token_file} - {e}")
            raise TokenValidationError(f"Token file contains invalid JSON: {e}")
        except ValueError as e:
            logger.error(f"TOKEN FILE INVALID: {e}")
            raise TokenValidationError(str(e))
        except Exception as e:
            logger.error(f"TOKEN FILE READ ERROR: {self.token_file} - {e}")
            raise TokenValidationError(f"Failed to read token file: {e}")

        self.access_token = record.access_token
        self.refresh_token = record.refresh_token

        # Log masked token values for debugging
        logger.info(f"Token file loaded successfully:")
        logger.info(f"  access_token: {self._mask_token(self.access_token)}")
        logger.info(f"  refresh_token: {self._mask_token(self.refresh_token)}")
        logger.info(f"  scopes: {record.scopes}")

    def _validate_and_refresh_if_needed(self):
        """Validate token with Twitch API and refresh if expired."""
        logger.info("Validating access token with Twitch API...")

        try:
            response = requests.get(
                "https://id.twitch.tv/oauth2/validate",
                headers={"Authorization": f"OAuth {self.access_token}"},
                timeout=30
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"TOKEN VALIDATION REQUEST FAILED: {e}")
            raise TokenValidationError(f"Failed to connect to Twitch API for token validation: {e}")

        if response.status_code == 200:
            data = response.json()
            expires_in = data.get("expires_in", 0)
            scopes = data.get("scopes", [])
            user_id = data.get("user_id", "unknown")
            login = data.get("login", "unknown")

            logger.info(f"TOKEN VALID:")
            logger.info(f"  user_id: {user_id}")
            logger.info(f"  login: {login}")
            logger.info(f"  scopes: {scopes}")
            logger.info(f"  expires_in: {expires_in}s ({expires_in // 3600}h {(expires_in % 3600) // 60}m)")

            # Check required scopes
            required_scopes = {"clips:edit"}
            missing_scopes = required_scopes - set(scopes)
            if missing_scopes:
                logger.error(f"TOKEN MISSING REQUIRED SCOPES: {missing_scopes}")
                logger.error("Please re-run seed_twitch_tokens.py with the correct scopes")
                raise TokenValidationError(f"Token missing required scopes: {missing_scopes}")

            # Proactively refresh if expiring soon (within 10 minutes)
            if expires_in < 600:
                logger.warning(f"Token expiring soon ({expires_in}s), refreshing proactively...")
                self._refresh()

        elif response.status_code == 401:
            logger.warning("Access token expired or invalid, attempting refresh...")
            try:
                self._refresh()
                # Validate the new token
                self._validate_and_refresh_if_needed()
            except Exception as e:
                logger.error(f"TOKEN REFRESH FAILED: {e}")
                logger.error("The refresh token may be invalid. Please re-run seed_twitch_tokens.py")
                raise TokenValidationError(f"Token expired and refresh failed: {e}")
        else:
            logger.error(f"TOKEN VALIDATION FAILED: status={response.status_code}, body={response.text}")
            raise TokenValidationError(f"Token validation failed with status {response.status_code}")

    def _refresh(self) -> None:
        """Refresh the access token via the shared credentials module, which
        holds a cross-process file lock across the read-refresh-write so a
        concurrent refresh from another container can't leave either side
        holding a dead token."""
        record = self._credentials.refresh(self.client_id, self.client_secret)
        self.access_token = record.access_token
        self.refresh_token = record.refresh_token

    def _is_retryable_status(self, status_code: int) -> bool:
        """Check if a status code indicates a retryable error."""
        return status_code in RETRYABLE_STATUS_CODES

    def create_clip(self, broadcaster_id: int) -> Optional[str]:
        """
        Create a clip for the given broadcaster. Returns clip ID if successful.

        Raises:
            TwitchAPIError: If the API returns an error (with is_retryable flag)
        """
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id
        }

        logger.info(f"Calling Twitch create clip API for broadcaster_id={broadcaster_id}")
        try:
            response = requests.post(
                "https://api.twitch.tv/helix/clips",
                headers=headers,
                params={"broadcaster_id": str(broadcaster_id)},
                timeout=30
            )
            logger.info(f"Create clip API response: status={response.status_code}, body={response.text[:500]}")

            if response.status_code == 202:
                data = response.json()
                if data.get("data"):
                    clip_id = data["data"][0]["id"]
                    logger.info(f"Clip creation accepted: clip_id={clip_id}")
                    return clip_id
                else:
                    logger.warning(f"Create clip returned 202 but no data: {response.text}")
                    return None
            elif response.status_code == 401:
                # Token expired - try refresh once
                logger.warning("Got 401, attempting token refresh...")
                self._refresh()
                # Retry with new token
                headers["Authorization"] = f"Bearer {self.access_token}"
                response = requests.post(
                    "https://api.twitch.tv/helix/clips",
                    headers=headers,
                    params={"broadcaster_id": str(broadcaster_id)},
                    timeout=30
                )
                logger.info(f"Retry after refresh: status={response.status_code}, body={response.text[:500]}")
                if response.status_code == 202:
                    data = response.json()
                    if data.get("data"):
                        clip_id = data["data"][0]["id"]
                        logger.info(f"Clip creation accepted after refresh: clip_id={clip_id}")
                        return clip_id
                # Still failing after refresh - not retryable
                raise TwitchAPIError(
                    f"Create clip failed after token refresh: {response.text}",
                    response.status_code,
                    is_retryable=False
                )
            else:
                is_retryable = self._is_retryable_status(response.status_code)
                raise TwitchAPIError(
                    f"Create clip failed: status={response.status_code}, body={response.text}",
                    response.status_code,
                    is_retryable=is_retryable
                )
        except TwitchAPIError:
            raise
        except requests.exceptions.Timeout:
            raise TwitchAPIError("Request timed out", 408, is_retryable=True)
        except requests.exceptions.ConnectionError as e:
            raise TwitchAPIError(f"Connection error: {e}", 0, is_retryable=True)
        except Exception as e:
            logger.error(f"Create clip exception for broadcaster_id={broadcaster_id}: {e}")
            raise TwitchAPIError(f"Unexpected error: {e}", 0, is_retryable=False)

    def get_clip(self, clip_id: str) -> Optional[Dict]:
        """Get clip details. Returns clip data if found."""
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id
        }

        logger.info(f"Fetching clip metadata for clip_id={clip_id}")
        try:
            response = requests.get(
                "https://api.twitch.tv/helix/clips",
                headers=headers,
                params={"id": clip_id},
                timeout=30
            )
            logger.info(f"Get clip API response: status={response.status_code}")

            if response.status_code == 200:
                data = response.json()
                if data.get("data"):
                    logger.info(f"Clip metadata retrieved: embed_url={data['data'][0].get('embed_url', 'N/A')[:50]}...")
                    return data["data"][0]
                else:
                    # Expected while Twitch is still processing the clip -- the
                    # caller retries; this alone isn't a failure.
                    logger.info(f"Get clip returned 200 but no data yet for clip_id={clip_id}")
            elif response.status_code == 401:
                # Token expired - refresh and retry
                logger.warning("Got 401 on get_clip, attempting token refresh...")
                self._refresh()
                headers["Authorization"] = f"Bearer {self.access_token}"
                response = requests.get(
                    "https://api.twitch.tv/helix/clips",
                    headers=headers,
                    params={"id": clip_id},
                    timeout=30
                )
                if response.status_code == 200:
                    data = response.json()
                    if data.get("data"):
                        return data["data"][0]
            else:
                logger.error(f"Get clip failed: status={response.status_code}, body={response.text}")
        except Exception as e:
            logger.error(f"Get clip exception for clip_id={clip_id}: {e}")
        return None


class PostgresClient:
    """Client for storing clips in Postgres."""

    def __init__(self, host: str, port: str, database: str, user: str, password: str):
        self.connection_params = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password
        }
        self._conn = None

    def _get_connection(self):
        """Get or create database connection."""
        import psycopg2
        if self._conn is None or self._conn.closed:
            logger.info(f"Connecting to Postgres: host={self.connection_params['host']}, db={self.connection_params['database']}")
            try:
                self._conn = psycopg2.connect(**self.connection_params)
                logger.info("Postgres connection established successfully")
            except Exception as e:
                logger.error(f"Postgres connection failed: {e}")
                raise
        return self._conn

    def insert_clip(self, clip: ClipResult):
        """Insert a clip into the database."""
        logger.info(f"Inserting clip into database: clip_id={clip.clip_id}, broadcaster_id={clip.broadcaster_id}, intensity={clip.intensity}")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO clips (broadcaster_id, clip_id, embed_url, thumbnail_url, detected_at, intensity, duration, vod_offset)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (clip_id) DO NOTHING
                """, (
                    clip.broadcaster_id,
                    clip.clip_id,
                    clip.embed_url,
                    clip.thumbnail_url,
                    datetime.fromtimestamp(clip.detected_at / 1000, tz=timezone.utc),
                    clip.intensity,
                    clip.duration,
                    clip.vod_offset
                ))
                rows_affected = cur.rowcount
                conn.commit()
                if rows_affected > 0:
                    logger.info(f"Successfully inserted clip {clip.clip_id} for broadcaster {clip.broadcaster_id} with intensity {clip.intensity}")
                else:
                    logger.warning(f"Clip {clip.clip_id} already exists (conflict), no insert performed")
        except Exception as e:
            logger.error(f"Failed to insert clip {clip.clip_id}: {e}")
            conn.rollback()
            raise

    def mark_clipping_disabled(self, broadcaster_id: int):
        """Record that a broadcaster does not allow clip creation.

        stream-monitoring checks this flag before joining/staying in a
        broadcaster's chat, so we stop spending a chat connection on
        someone we can never successfully clip.
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE streamers SET allows_clipping = FALSE WHERE streamer_id = %s
                """, (broadcaster_id,))
                conn.commit()
                logger.info(f"Marked broadcaster {broadcaster_id} as allows_clipping=FALSE")
        except Exception as e:
            logger.error(f"Failed to mark broadcaster {broadcaster_id} as clipping-disabled: {e}")
            conn.rollback()

    def close(self):
        """Close the database connection."""
        if self._conn:
            self._conn.close()


class CommandFilter(ProcessFunction):
    """Filters out command messages (starting with !)."""

    def process_element(self, value: str, ctx: ProcessFunction.Context) -> Iterator[str]:
        try:
            msg = json.loads(value)
            text = msg.get("text", "")
            if not is_command(text):
                yield value
        except json.JSONDecodeError:
            pass


class SentAtTimestampAssigner(TimestampAssigner):
    """
    Assigns event time from `sent_at` (Twitch's own clock), not `timestamp`
    (our ingestion clock). Plan 06 Phase 2 -- see AnomalyDetector below,
    which buckets and schedules its per-second timers off this, via
    ctx.timestamp() / the watermark it drives.
    """

    def extract_timestamp(self, value: str, record_timestamp: int) -> int:
        try:
            sent_at = json.loads(value)["sent_at"]
        except (json.JSONDecodeError, KeyError):
            return record_timestamp
        # sent_at is present-but-null, not just missing: fall back rather
        # than handing None to Flink's timestamp assignment, which expects
        # an int and isn't guarded against it.
        return record_timestamp if sent_at is None else sent_at


class AnomalyDetector(KeyedProcessFunction):
    """
    Adapter that feeds Flink's keyed MapState/ValueState into spike_detector.evaluate()
    and applies the resulting Decision. See DetectorConfig for the tuning.

    Event time throughout (Plan 06 Phase 2): process_element only buckets the
    incoming message and arms a timer; evaluate() itself runs once per
    elapsed event-time second from on_timer, when the watermark -- built
    from sent_at by SentAtTimestampAssigner, below -- passes that second.
    Per-message evaluation depends on message interleaving, so it can't
    replay deterministically; per-second timers can. See
    tools/replay.py for the pure-Python equivalent this mirrors.

    Peak-hold (Plan 06 Phase 3): a spike no longer emits the instant it
    crosses the trigger. evaluate() opens a hold, tracks the highest intensity
    while chat stays elevated, and emits once the episode ends or its cap
    expires -- carrying the peak's value and the peak's timestamp. This
    operator's job is to persist that hold between seconds and to stamp the
    emitted anomaly with the peak's second rather than the firing second.
    """

    def __init__(self):
        self.message_counts = None  # MapState: event-time second (sent_at bucket) -> count
        self.hold = None  # ValueState: HoldState as JSON, or null when no episode is open
        self.last_fire_second = None  # ValueState: event-time second of the last emit
        self.config = None
        self.subtask_index = 0

    def _state_ttl(self):
        """
        TTL for this operator's keyed state (Plan 06 step 16).

        message_counts and the value states below are otherwise pruned only
        when a message arrives for that key, so a broadcaster who goes offline
        leaves their buckets behind for the life of the job -- small per key,
        unbounded over time. This is the correct fix for that leak; the
        stream-lifecycle topic looked like it was meant to solve it and is
        being removed instead.

        Twice the baseline window: comfortably longer than any live key's gap
        between writes (an active key rewrites its buckets every second), and
        short enough that an idle key is cleaned up promptly.

        NeverReturnExpired matters for `hold` specifically. If the whole
        pipeline goes quiet with an episode open, watermarks stall, no timer
        fires and the hold is stranded in state -- see the event-time caveat in
        plans/06-detection-math.md. This guarantees that when traffic returns,
        a stranded hold reads back as absent rather than firing a clip for a
        peak that happened hours ago.
        """
        return (
            StateTtlConfig
            .new_builder(Time.seconds(self.config.baseline_seconds * 2))
            .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
            .set_state_visibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
            .cleanup_incrementally(10, True)
            .build()
        )

    def open(self, runtime_context):
        # Start the metrics server here so it comes up on the first chat
        # message this worker processes, not the first anomaly (previously
        # it only started inside the anomaly branch -> the /metrics endpoint
        # stayed dark, tripping ClipDetectorMetricsDown, through any quiet
        # stretch with no spikes -- much more likely now that
        # STD_DEV_THRESHOLD is 5.0 instead of 1.0).
        # Must be here, not at module scope: module-level start_http_server()
        # runs on the jobmanager during job submission too, and pollutes the
        # driver process with an unpicklable thread lock before cloudpickle
        # ships AnomalyDetector() to the task managers (breaks submission
        # entirely: "TypeError: cannot pickle '_thread.lock' object").
        self.subtask_index = runtime_context.get_index_of_this_subtask()
        _init_metrics(self.subtask_index)
        self.config = DetectorConfig.from_env()

        ttl_config = self._state_ttl()

        counts_descriptor = MapStateDescriptor("message_counts", Types.LONG(), Types.INT())
        counts_descriptor.enable_time_to_live(ttl_config)
        self.message_counts = runtime_context.get_map_state(counts_descriptor)

        # Flink has no TypeInformation for a dataclass, so the hold travels as
        # JSON -- HoldState.to_json/from_json own the encoding.
        hold_descriptor = ValueStateDescriptor("hold", Types.STRING())
        hold_descriptor.enable_time_to_live(ttl_config)
        self.hold = runtime_context.get_state(hold_descriptor)

        # Renamed from "last_anomaly_time", which held event-time milliseconds.
        # This holds event-time *seconds*, matching evaluate()'s signature. The
        # rename is deliberate: a same-named state would have silently restored
        # milliseconds into a seconds field. (Checkpointing is disabled on this
        # job anyway -- see flink-conf.yaml -- so nothing is restored today.)
        last_fire_descriptor = ValueStateDescriptor("last_fire_second", Types.LONG())
        last_fire_descriptor.enable_time_to_live(ttl_config)
        self.last_fire_second = runtime_context.get_state(last_fire_descriptor)

    def process_element(self, value, ctx: KeyedProcessFunction.Context) -> None:
        try:
            # ctx.timestamp() is sent_at (Twitch's own clock) -- assigned by
            # SentAtTimestampAssigner on the source's WatermarkStrategy, not
            # our ingestion timestamp and not wall-clock time.
            bucket = ctx.timestamp() // 1000

            current_count = self.message_counts.get(bucket)
            if current_count is None:
                current_count = 0
            self.message_counts.put(bucket, current_count + 1)

            # Fire once this second's watermark passes, via on_timer below --
            # not once per message. Registering the same timestamp twice is
            # a no-op in Flink, so it's safe to call on every message.
            ctx.timer_service().register_event_time_timer(bucket * 1000)
        except Exception as e:
            logger.error(
                f"Error updating message counts for broadcaster {ctx.get_current_key()}: {e}",
                exc_info=True,
            )

    def on_timer(self, timestamp: int, ctx: KeyedProcessFunction.OnTimerContext) -> Iterator[str]:
        broadcaster_id = ctx.get_current_key()
        try:
            now_seconds = timestamp // 1000
            all_counts = {ts: self.message_counts.get(ts) for ts in self.message_counts.keys()}

            # A message for second now_seconds+1..+5 can already be in
            # MapState by the time now_seconds's timer fires -- its own timer
            # only requires the watermark to pass now_seconds, but the
            # watermark itself only advances that far once messages up to
            # ~now_seconds + WATERMARK_OUT_OF_ORDERNESS_SECONDS have already
            # arrived and been counted in process_element. evaluate() has no
            # upper bound on ts_bucket (that invariant used to be guaranteed
            # by the caller, back when now_seconds was real wall-clock time
            # and no bucket could exceed it) so without this filter, future
            # buckets already sitting in state would leak into "now_seconds"'s
            # baseline/window.
            counts_as_of_now = {ts: c for ts, c in all_counts.items() if ts <= now_seconds}
            hold = HoldState.from_json(self.hold.value())
            decision = evaluate(
                counts_as_of_now,
                now_seconds,
                hold,
                self.last_fire_second.value(),
                self.config,
            )

            for expired_bucket in decision.expired_buckets:
                self.message_counts.remove(expired_bucket)
                all_counts.pop(expired_bucket, None)

            # Persist the elevation episode across seconds. Only write on a
            # change: an open hold is re-read every second and would otherwise
            # rewrite identical state 60 times per episode.
            if decision.hold != hold:
                if decision.hold is None:
                    self.hold.clear()
                else:
                    self.hold.update(decision.hold.to_json())

            # Keep the per-second cadence going only while this key still has
            # data in its baseline -- an idle broadcaster's chain lapses here
            # and a later message restarts it from process_element. Checked
            # against all_counts (not counts_as_of_now): a future bucket that
            # was excluded above still needs its own future timer.
            #
            # An open hold is normally bounded by its cap well before this
            # chain can lapse -- DetectorConfig enforces hold_cap_seconds below
            # baseline_seconds + window_seconds (60 vs 305 by default), and
            # buckets outlive the last message by that whole span. Two cases
            # escape that bound, and the state TTL above is the backstop for
            # both: a stalled watermark (no timer fires at all), and a baseline
            # that stops being measurable mid-episode, which by design leaves
            # the hold untouched rather than guessing at a peak.
            if all_counts:
                ctx.timer_service().register_event_time_timer(timestamp + 1000)

            if decision.emit is not None:
                spike = decision.emit
                # The cooldown runs from the moment we fire, which is now --
                # not from the peak this carries, which may be up to
                # hold_cap_seconds earlier.
                self.last_fire_second.update(now_seconds)

                # detected_at is the peak's second, not this one. Plan 06
                # Phase 3: the clips table now records when chat actually
                # peaked, instead of when the detector noticed the episode had
                # ended. Every other field below is from that same second too.
                #
                # Note this widens the gap between detected_at and what the
                # clip actually contains: ClipCreator still asks Twitch for
                # "the last ~30 seconds" as of when it runs, so an episode that
                # held for a while is clipped that much after its peak.
                # Re-anchoring clip timing on the peak needs Plan 04's
                # duration/vod_offset measurements and is deliberately out of
                # scope here -- see plans/06-detection-math.md, Out of scope.
                anomaly = {
                    "broadcaster_id": broadcaster_id,
                    "detected_at": spike.detected_at_seconds * 1000,
                    "message_count": spike.message_count,
                    "baseline_mean": spike.baseline_mean,
                    "baseline_std": spike.baseline_std,
                    "intensity": spike.intensity
                }
                logger.info(f"ANOMALY DETECTED for broadcaster {broadcaster_id}: "
                           f"intensity={spike.intensity:.2f} (trigger k={self.config.k}), "
                           f"peaked at {spike.detected_at_seconds} ({now_seconds - spike.detected_at_seconds}s ago), "
                           f"count={spike.message_count}, mean={spike.baseline_mean:.2f}, "
                           f"std={spike.baseline_std:.2f}")
                _init_metrics(self.subtask_index)
                if _anomalies_detected_total:
                    _anomalies_detected_total.labels(broadcaster_id=str(broadcaster_id)).inc()
                yield json.dumps(anomaly)

        except Exception as e:
            logger.error(f"Error in anomaly detection for broadcaster {broadcaster_id}: {e}", exc_info=True)


class ClipCreator(ProcessFunction):
    """
    Creates clips via Twitch API when anomalies are detected.
    Implements smart retry logic - only retries transient errors (timeouts, 5xx, 429).
    Non-retryable errors (4xx except 429) fail immediately.
    """

    def __init__(self):
        self.twitch_client = None
        self.postgres_client = None
        self.subtask_index = 0

    def open(self, runtime_context):
        self.subtask_index = runtime_context.get_index_of_this_subtask()
        self.twitch_client = TwitchAPIClient(
            TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_TOKEN_FILE
        )
        self.postgres_client = PostgresClient(
            POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
            POSTGRES_USER, POSTGRES_PASSWORD
        )
        self.clip_policy = ClipPolicy.from_env()
        # The single Postgres connection and the lazily-initialized Prometheus
        # metrics can't handle concurrent use from multiple clip threads --
        # these now guard only the apply step below (the DB write and the
        # metrics update), not the long sleeps that precede it.
        self._postgres_lock = threading.Lock()
        self._metrics_lock = threading.Lock()

    def close(self):
        if self.postgres_client:
            self.postgres_client.close()

    def process_element(self, value: str, ctx: ProcessFunction.Context) -> Iterator[str]:
        try:
            anomaly = json.loads(value)
            broadcaster_id = anomaly["broadcaster_id"]
            detected_at = anomaly["detected_at"]
        except Exception as e:
            logger.error(f"CLIP CREATION ERROR for value={value[:200]}: {e}", exc_info=True)
            return iter(())

        # The full flow below can sleep for the better part of half an hour
        # (ClipPolicy's initial delay + retry delays + metadata retry delays)
        # waiting on Twitch. Running it inline on process_element would block this
        # subtask's task thread for that whole time -- starving every other
        # broadcaster keyed onto the same subtask, and even a second anomaly
        # for this same broadcaster, until it finished. Run it on its own
        # thread instead so it only ever holds up itself.
        threading.Thread(
            target=self._create_and_poll_clip,
            args=(anomaly, broadcaster_id, detected_at),
            name=f"clip-creator-{broadcaster_id}-{detected_at}",
            daemon=True,
        ).start()
        return iter(())

    def _create_and_poll_clip(self, anomaly: dict, broadcaster_id, detected_at) -> None:
        try:
            logger.info(f"=== CLIP CREATION START for broadcaster {broadcaster_id} ===")
            logger.info(f"Anomaly details: count={anomaly.get('message_count')}, "
                       f"mean={anomaly.get('baseline_mean', 0):.2f}, std={anomaly.get('baseline_std', 0):.2f}")

            attempt = ClipAttempt(self.twitch_client, self.clip_policy, RealClock())
            result = attempt.run(broadcaster_id)
            self._apply_result(anomaly, broadcaster_id, detected_at, result)
        except Exception as e:
            logger.error(f"CLIP CREATION ERROR for broadcaster {broadcaster_id}: {e}", exc_info=True)

    def _apply_result(self, anomaly: dict, broadcaster_id, detected_at, result) -> None:
        if result.failure_reason:
            # ClipAttempt already logged the retry/poll detail and the failure
            # reason for broadcaster_id -- this is just the metrics/signal apply step.
            with self._metrics_lock:
                _init_metrics(self.subtask_index)
            if _clips_created_failed_total:
                _clips_created_failed_total.labels(broadcaster_id=str(broadcaster_id), reason=result.failure_reason).inc()
            # Twitch returns 403 here specifically when the broadcaster hasn't
            # authorized clip creation on their channel -- that's permanent
            # until they change it, not something a retry or token refresh
            # fixes. Record it so stream-monitoring stops watching their chat.
            if result.clipping_disabled:
                logger.warning(f"Broadcaster {broadcaster_id} does not authorize clip creation; marking allows_clipping=FALSE")
                with self._postgres_lock:
                    self.postgres_client.mark_clipping_disabled(broadcaster_id)
            return

        clip_data = result.clip_data
        intensity = anomaly.get("intensity")

        clip_result = ClipResult(
            broadcaster_id=broadcaster_id,
            clip_id=result.clip_id,
            embed_url=clip_data.get("embed_url", ""),
            thumbnail_url=clip_data.get("thumbnail_url", ""),
            detected_at=detected_at,
            success=True,
            intensity=intensity,
            duration=clip_data.get("duration"),
            vod_offset=clip_data.get("vod_offset"),
        )

        logger.info(f"Storing clip {result.clip_id} in database...")
        with self._postgres_lock:
            self.postgres_client.insert_clip(clip_result)

        with self._metrics_lock:
            _init_metrics(self.subtask_index)
        if _clips_created_success_total:
            _clips_created_success_total.labels(broadcaster_id=str(broadcaster_id)).inc()
        if _clip_creation_duration_seconds:
            _clip_creation_duration_seconds.labels(broadcaster_id=str(broadcaster_id)).set(result.duration_seconds)

        # Nothing downstream consumes ClipCreator's old yielded output
        # besides clips.print() (a debug echo) -- this log line is the
        # durable record, alongside the Postgres row and metrics above.
        logger.info(f"=== CLIP CREATION COMPLETE for broadcaster {broadcaster_id}: "
                    f"clip_id={result.clip_id} (took {result.duration_seconds:.1f}s) ===")


def validate_tokens_at_startup():
    """
    Validate Twitch tokens before starting the Flink job.
    Fails fast with clear error messages if tokens are invalid.
    """
    logger.info("=" * 60)
    logger.info("STARTUP TOKEN VALIDATION")
    logger.info("=" * 60)

    # Check for required credentials
    if not TWITCH_CLIENT_ID:
        logger.error("TWITCH_CLIENT_ID environment variable is not set")
        raise TokenValidationError("TWITCH_CLIENT_ID is required but not set")
    if not TWITCH_CLIENT_SECRET:
        logger.error("TWITCH_CLIENT_SECRET environment variable is not set")
        raise TokenValidationError("TWITCH_CLIENT_SECRET is required but not set")

    # Create client with validation (will raise TokenValidationError on failure)
    try:
        client = TwitchAPIClient(
            TWITCH_CLIENT_ID,
            TWITCH_CLIENT_SECRET,
            TWITCH_TOKEN_FILE,
            validate_on_init=True
        )
        logger.info("=" * 60)
        logger.info("TOKEN VALIDATION SUCCESSFUL - Ready to create clips")
        logger.info("=" * 60)
        return client
    except TokenValidationError:
        raise
    except Exception as e:
        logger.error(f"Unexpected error during token validation: {e}")
        raise TokenValidationError(f"Token validation failed: {e}")


def main():
    """Main entry point for the Flink job."""
    detector_config = DetectorConfig.from_env()
    logger.info("=" * 60)
    logger.info("Starting Clip Detector Job")
    logger.info("=" * 60)
    logger.info(f"Configuration:")
    logger.info(f"  KAFKA_BOOTSTRAP_SERVERS: {KAFKA_BOOTSTRAP_SERVERS}")
    logger.info(f"  POSTGRES_HOST: {POSTGRES_HOST}")
    logger.info(f"  FLINK_PARALLELISM: {FLINK_PARALLELISM}")
    logger.info(f"  DETECTION_WINDOW_SECONDS: {detector_config.window_seconds}")
    logger.info(f"  DETECTION_BASELINE_SECONDS: {detector_config.baseline_seconds}")
    # DetectorConfig calls this field `k`; the environment variable keeps its
    # original name, which spec 002 FR-001b and docker-compose.yml refer to.
    logger.info(f"  DETECTION_STD_DEV_THRESHOLD: {detector_config.k}")
    logger.info(f"  DETECTION_HOLD_CAP_SECONDS: {detector_config.hold_cap_seconds}")
    logger.info(f"  DETECTION_COOLDOWN_SECONDS: {detector_config.cooldown_seconds}")
    logger.info(f"  TWITCH_CLIENT_ID: {'set' if TWITCH_CLIENT_ID else 'NOT SET'}")
    logger.info(f"  TWITCH_CLIENT_SECRET: {'set' if TWITCH_CLIENT_SECRET else 'NOT SET'}")
    logger.info(f"  TWITCH_TOKEN_FILE: {TWITCH_TOKEN_FILE}")
    logger.info(f"  RETRYABLE_STATUS_CODES: {RETRYABLE_STATUS_CODES}")

    # Validate tokens before starting the pipeline
    validate_tokens_at_startup()

    # Set up execution environment
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(FLINK_PARALLELISM)

    # Configure Kafka source
    kafka_source = KafkaSource.builder() \
        .set_bootstrap_servers(KAFKA_BOOTSTRAP_SERVERS) \
        .set_topics("chat-messages") \
        .set_group_id("clip-detector") \
        .set_starting_offsets(KafkaOffsetsInitializer.latest()) \
        .set_value_only_deserializer(SimpleStringSchema()) \
        .build()

    # Create watermark strategy. Event time comes from sent_at (Twitch's own
    # clock) via SentAtTimestampAssigner -- AnomalyDetector's bucketing and
    # per-second timers ride on this, not on our ingestion timestamp or
    # wall-clock time. WATERMARK_OUT_OF_ORDERNESS_SECONDS is shared with
    # tools/replay.py so the harness simulates the same allowed lateness.
    watermark_strategy = WatermarkStrategy \
        .for_bounded_out_of_orderness(Duration.of_seconds(WATERMARK_OUT_OF_ORDERNESS_SECONDS)) \
        .with_idleness(Duration.of_minutes(1)) \
        .with_timestamp_assigner(SentAtTimestampAssigner())

    # Build the pipeline
    messages = env.from_source(
        kafka_source,
        watermark_strategy,
        "Kafka Source"
    )

    # Filter out command messages
    filtered = messages.process(CommandFilter())

    # Key by broadcaster_id and detect anomalies
    anomalies = filtered \
        .map(lambda x: (json.loads(x)["broadcaster_id"], x)) \
        .key_by(lambda x: x[0]) \
        .process(AnomalyDetector()) \
        .map(lambda x: x[1] if isinstance(x, tuple) else x)

    # Create clips for detected anomalies
    clips = anomalies.process(ClipCreator())

    # Log created clips
    clips.print()

    # Execute the job
    env.execute("Clip Detector Job")


if __name__ == "__main__":
    main()
