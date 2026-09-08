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
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterator, Optional, Tuple

import requests
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from pyflink.common import Row, Types, WatermarkStrategy
from pyflink.common.serialization import SimpleStringSchema
from pyflink.common.time import Duration, Time
from pyflink.common.watermark_strategy import TimestampAssigner
from pyflink.datastream import (
    KeyedCoProcessFunction,
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
    SuppressionConfig,
    SuppressionSourceSettings,
    SuppressionState,
    SUPPRESSION_LAG_LAGGING,
    SUPPRESSION_MAX_FUTURE_SKEW_SECONDS,
    SUPPRESSION_REJECT_FIELDS,
    WATERMARK_IDLENESS_SECONDS,
    WATERMARK_OUT_OF_ORDERNESS_SECONDS,
    apply_notice,
    decode_suppression_record,
    evaluate,
    is_command,
    is_suppressed,
    is_trustworthy_notice_time,
    next_chain_timer,
    observe_delivery_age,
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

# Feature 007. The key a suppression record routes to when its payload carries
# no usable broadcaster identity. Negative, so it can never collide with a
# Twitch broadcaster id: the record still has to reach process_element2 to be
# counted as rejected, and a keying function that raised -- or that dropped the
# record -- would make suppression_records_rejected_total structurally
# unreachable (contract section 4.1 rule 1).
SUPPRESSION_UNROUTABLE_KEY = -1

# Its own consumer group, distinct from the chat source's "clip-detector".
# Sharing one group id across two topics would make the two subscriptions'
# offsets and rebalances interfere for no benefit.
SUPPRESSION_CONSUMER_GROUP = "clip-detector-suppression"

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
_hold_regressed_total = None
# Feature 007 consumer-side signals. Only the reason/category labels are
# bounded by construction; clips_suppressed_total keeps broadcaster
# attribution because NFR-006 requires it, under the same finite
# monitored-channel label policy anomalies_detected_total already uses.
_clips_suppressed_total = None
_suppression_records_rejected_total = None
_suppression_records_consumed_total = None
_suppression_delivery_age_seconds = None


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
    global _clips_created_failed_total, _clip_creation_duration_seconds, _hold_regressed_total
    global _clips_suppressed_total, _suppression_records_rejected_total
    global _suppression_records_consumed_total, _suppression_delivery_age_seconds

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

    def get_or_create_histogram(name, desc, labels, buckets):
        for collector in list(REGISTRY._names_to_collectors.values()):
            if hasattr(collector, '_name') and collector._name == name:
                return collector
        return Histogram(name, desc, labels, buckets=buckets)

    try:
        _anomalies_detected_total = get_or_create_counter("anomalies_detected_total", "Total anomalies detected", ["broadcaster_id"])
        _clips_created_success_total = get_or_create_counter("clips_created_success_total", "Total clips created successfully", ["broadcaster_id"])
        _clips_created_failed_total = get_or_create_counter("clips_created_failed_total", "Total clip creation failures", ["broadcaster_id", "reason"])
        _clip_creation_duration_seconds = get_or_create_gauge("clip_creation_duration_seconds", "Time taken to create last clip", ["broadcaster_id"])
        # Plan 09 / KNOWN_ISSUES.md Issue 3. The guard that fixed the
        # duplicate-clip bug also removed the evidence that showed the bug: a
        # regressed hold no longer emits, so it no longer logs. This counter
        # is now the only production signal for one open question: why does
        # the cursor regress at all?
        _hold_regressed_total = get_or_create_counter("hold_regressed_total", "Total holds passed through with peak ahead of cursor", ["broadcaster_id"])

        # Feature 007 (FR-012, NFR-004, NFR-005, NFR-006). One increment per
        # would-have-clipped spike that the gate stopped, attributable to the
        # channel and to the notice category that opened the window.
        _clips_suppressed_total = get_or_create_counter(
            "clips_suppressed_total",
            "Total would-have-clipped spikes suppressed by a gift/raid window",
            ["broadcaster_id", "notice_type"],
        )
        # The reason label is closed by construction: it is always one of
        # spike_detector.SUPPRESSION_REJECT_REASONS.
        _suppression_records_rejected_total = get_or_create_counter(
            "suppression_records_rejected_total",
            "Total suppression-events records ignored, by rejection reason",
            ["reason"],
        )
        # Two classes and no third one for silence. A window with no record
        # publishes nothing at all and reads as idle/unknown in Prometheus,
        # because any value published during legitimate silence would be
        # invented (research D13, decision 20).
        _suppression_records_consumed_total = get_or_create_counter(
            "suppression_records_consumed_total",
            "Total suppression-events records applied, by delivery lag class",
            ["lag_class"],
        )
        # Unlabelled on purpose: this is a distribution of delivery age over
        # received records, not a per-channel level.
        _suppression_delivery_age_seconds = get_or_create_histogram(
            "suppression_delivery_age_seconds",
            "Delivery age of consumed suppression-events records, in seconds",
            [],
            (0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, float("inf")),
        )

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
        """Refresh the access token via the shared credentials module.

        refresh() holds a cross-process file lock across the whole
        read-refresh-write and reads the *current* refresh token from disk
        inside that lock, so a refresh racing from stream-monitoring can't
        leave either side spending a rotated-away refresh token. It returns
        the freshly minted token even if it could not be written back to
        secrets/ (logged loudly), so a read-only secrets/ degrades to
        "works until this token expires" rather than an immediate hard
        failure."""
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

        stream-monitoring drops this broadcaster from the ranking, so we stop
        spending a subscription on someone we can never successfully clip.

        `clipping_disabled_at` is written in the same UPDATE as the boolean, so
        the two can never disagree. The timestamp is what lets the skip expire:
        stream-monitoring lets a broadcaster back into the ranking once the
        mark is more than 7 days old, which gives a broadcaster who has since
        turned clipping on a way back (spec 004 D5, FR-013). Without it the
        boolean is a life sentence.
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE streamers
                    SET allows_clipping = FALSE, clipping_disabled_at = NOW()
                    WHERE streamer_id = %s
                """, (broadcaster_id,))
                conn.commit()
                logger.info(f"Marked broadcaster {broadcaster_id} as allows_clipping=FALSE")
        except Exception as e:
            logger.error(f"Failed to mark broadcaster {broadcaster_id} as clipping-disabled: {e}")
            conn.rollback()

    def mark_clipping_allowed(self, broadcaster_id: int):
        """Undo a `allows_clipping = FALSE` mark after a clip actually worked.

        Only touches rows that are currently FALSE, so the ordinary case --
        a broadcaster who has always allowed clipping -- costs one UPDATE that
        matches nothing rather than a write per clip. The 7-day re-check hands
        a stale-disabled broadcaster one more attempt; this is what happens
        when that attempt succeeds.
        """
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE streamers
                    SET allows_clipping = TRUE, clipping_disabled_at = NULL
                    WHERE streamer_id = %s AND allows_clipping = FALSE
                """, (broadcaster_id,))
                healed = cur.rowcount
                conn.commit()
            if healed:
                logger.info(f"Broadcaster {broadcaster_id} allows clipping again; cleared the mark")
        except Exception as e:
            logger.error(f"Failed to clear the clipping-disabled mark for {broadcaster_id}: {e}")
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

    def __init__(self, clock_ms=None):
        self._clock_ms = clock_ms

    def _source_clock_ms(self) -> int:
        if self._clock_ms is not None:
            return int(self._clock_ms())
        return int(time.time() * 1000)

    def extract_timestamp(self, value: str, record_timestamp: int) -> int:
        try:
            sent_at = json.loads(value)["sent_at"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return record_timestamp
        # Chat is never dropped for an untrustworthy timestamp. Use Kafka
        # record time for event-time assignment instead, preventing one bad
        # value from poisoning the binding chat watermark while preserving the
        # message itself for counting and detection.
        if not _plain_int(sent_at) or not is_trustworthy_notice_time(
            sent_at, self._source_clock_ms()
        ):
            return record_timestamp
        return sent_at


def _suppression_payload(value) -> Optional[dict]:
    """The record as a dict, or None for anything that is not one. Never raises.

    Both callers below run before the operator does, on the source side, where
    an exception would fail the job rather than a single record.
    """
    try:
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8")
        payload = json.loads(value)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _plain_int(value) -> Optional[int]:
    """The value when it is an int that is not a bool, else None.

    bool subclasses int and True == 1, so a `broadcaster_id` of `false` would
    otherwise route as channel 0 (contract section 4.1 rule 3).
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def suppression_key(value) -> int:
    """The key one `suppression-events` value routes to.

    The payload `broadcaster_id`, because that is the only identity the
    consumer can see: the source deserializes values only, so the Kafka key
    never reaches the operator, and key/payload agreement is a producer
    invariant asserted where the key exists (contract section 4.0, research
    D15).

    A record this cannot route still has to reach process_element2, which is
    what counts it as rejected. Returning SUPPRESSION_UNROUTABLE_KEY rather
    than raising or dropping is what keeps that counter reachable. A record
    that carries a usable id but fails some other check -- an unknown
    `schema_version`, an excluded `notice_type` -- keeps its own channel's key,
    so rejections spread across the keyspace instead of piling onto one key.
    """
    decoded = decode_suppression_record(value)
    if decoded.notice is not None:
        return decoded.notice.broadcaster_id

    payload = _suppression_payload(value)
    if payload is None:
        return SUPPRESSION_UNROUTABLE_KEY
    broadcaster_id = _plain_int(payload.get("broadcaster_id"))
    return SUPPRESSION_UNROUTABLE_KEY if broadcaster_id is None else broadcaster_id


class SuppressionTimestampAssigner(TimestampAssigner):
    """
    Assigns event time from `occurred_at_ms` -- Twitch's own clock, produced by
    the same converter that stamps `chat-messages.sent_at`. That shared clock
    is what makes
    `suppress_from_ms <= peak_second * 1000 < suppress_until_ms` meaningful
    (contract invariant 2).

    A record with no usable or trustworthy time falls back to the Kafka record
    timestamp. The original payload still reaches process_element2 and is
    rejected there; this fallback only prevents an untrusted future value from
    irreversibly advancing the source watermark before that rejection.
    """

    def __init__(self, clock_ms=None):
        self._clock_ms = clock_ms

    def _source_clock_ms(self) -> int:
        if self._clock_ms is not None:
            return int(self._clock_ms())
        return int(time.time() * 1000)

    def extract_timestamp(self, value, record_timestamp: int) -> int:
        payload = _suppression_payload(value)
        if payload is None:
            return record_timestamp
        occurred_at_ms = _plain_int(payload.get("occurred_at_ms"))
        if occurred_at_ms is None or not is_trustworthy_notice_time(
            occurred_at_ms, self._source_clock_ms()
        ):
            return record_timestamp
        return occurred_at_ms


def build_suppression_source(settings: SuppressionSourceSettings):
    """The second Kafka source, built entirely from the pure settings object.

    Nothing here is a literal, so the values that decide watermark behaviour
    can be asserted with no PyFlink installed and no broker running
    (research D16). latest() rather than earliest(): replaying hours-old
    `occurred_at_ms` into event time would pin the operator watermark in the
    past and stall detection for every channel (research D4).
    """
    offsets = (
        KafkaOffsetsInitializer.latest()
        if settings.starting_offsets == "latest"
        else KafkaOffsetsInitializer.earliest()
    )
    return KafkaSource.builder() \
        .set_bootstrap_servers(KAFKA_BOOTSTRAP_SERVERS) \
        .set_topics(settings.topic) \
        .set_group_id(SUPPRESSION_CONSUMER_GROUP) \
        .set_starting_offsets(offsets) \
        .set_value_only_deserializer(SimpleStringSchema()) \
        .build()


def build_chat_watermark_strategy():
    """The chat stream's real strategy, built once and attached post-source.

    Event time comes from sent_at (Twitch's own clock) via
    SentAtTimestampAssigner -- AnomalyDetector's bucketing and per-second
    timers ride on this, not on our ingestion timestamp or wall-clock time.
    WATERMARK_OUT_OF_ORDERNESS_SECONDS is shared with tools/replay.py so the
    harness simulates the same allowed lateness.

    WATERMARK_IDLENESS_SECONDS (KNOWN_ISSUES.md Issue 4): an input can go quiet
    for a long stretch and freeze the operator watermark -- the minimum across
    every input -- until this timeout releases it. Through 2026-08-27 the topic
    ran with far more partitions than this job's parallelism, so most splits
    carried 0-1 broadcasters and this fired routinely; that mismatch is fixed
    (chat-messages now matches FLINK_PARALLELISM 1:1, see docker-compose.yml),
    but a single broadcaster's own input can still go quiet on its own
    regardless of partition count, and this timeout is still what recovers
    from it.
    """
    return WatermarkStrategy \
        .for_bounded_out_of_orderness(Duration.of_seconds(WATERMARK_OUT_OF_ORDERNESS_SECONDS)) \
        .with_idleness(Duration.of_seconds(WATERMARK_IDLENESS_SECONDS)) \
        .with_timestamp_assigner(SentAtTimestampAssigner())


def build_suppression_watermark_strategy(settings: SuppressionSourceSettings):
    """Real watermarks on the sparse input, never no_watermarks().

    A two-input operator's watermark is the minimum of both inputs, so a
    suppression stream that never advances would stop every broadcaster's
    detection -- far worse than the false-positive clips this feature removes.
    The idleness timeout is deliberately below the chat stream's
    WATERMARK_IDLENESS_SECONDS, so this input is never the last split released
    and never the binding minimum in steady state (research section 4.1, I15).
    """
    return WatermarkStrategy \
        .for_bounded_out_of_orderness(Duration.of_seconds(settings.out_of_orderness_seconds)) \
        .with_idleness(Duration.of_seconds(settings.idleness_seconds)) \
        .with_timestamp_assigner(SuppressionTimestampAssigner())


def build_event_time_streams(env, chat_source, suppression_source, settings):
    """Both sources entered with no_watermarks(), then immediately given their
    real strategy on the returned stream (contract section 1.1.1, decision 25).

    PyFlink 1.18's StreamExecutionEnvironment.from_source() forwards only
    watermark_strategy._j_watermark_strategy, so a Python TimestampAssigner
    handed to from_source is silently discarded -- no error, no warning -- and
    event time degrades to the Kafka record timestamp on both inputs. Only
    DataStream.assign_timestamps_and_watermarks() installs the executable
    Python timestamp-assigner/watermark-generator operator, which is why the
    strategy given to from_source here is a placeholder and the real strategy
    is attached on the very next call. That is also what keeps
    SuppressionTimestampAssigner's +30 s source trust check reachable ahead of
    the independent rejection in process_element2.

    Nothing may come between a from_source and its assignment: idleness is now
    generated per assignment subtask rather than per Kafka split, so the
    equivalence relies on topic partitions = source parallelism = assignment
    parallelism = 4 over a one-to-one forward chain (research section 4.1.2,
    R13). Any map, key_by, rescale, repartition, or explicit parallelism
    between the two calls breaks that and requires revalidation; both streams
    therefore stay on the job-wide FLINK_PARALLELISM.
    """
    chat_stream = env.from_source(
        chat_source,
        WatermarkStrategy.no_watermarks(),
        "Kafka Source"
    ).assign_timestamps_and_watermarks(build_chat_watermark_strategy())

    suppression_stream = env.from_source(
        suppression_source,
        WatermarkStrategy.no_watermarks(),
        "Suppression Source"
    ).assign_timestamps_and_watermarks(build_suppression_watermark_strategy(settings))

    return chat_stream, suppression_stream


def connect_detector(chat_stream, suppression_stream, detector):
    """Key both inputs on the payload broadcaster_id, then connect them.

    Keying both sides on the same value is what makes the suppression state
    channel-isolated structurally rather than by convention (NFR-002): the
    operator only ever sees the state Flink bound for the current key, and
    there is no map from broadcaster to state anywhere in the operator.

    CommandFilter and ClipCreator are not in this path and do not change.
    """
    chat_keyed = chat_stream \
        .map(lambda x: (json.loads(x)["broadcaster_id"], x)) \
        .key_by(lambda x: x[0])
    suppression_keyed = suppression_stream \
        .map(lambda x: (suppression_key(x), x)) \
        .key_by(lambda x: x[0])
    return chat_keyed.connect(suppression_keyed).process(detector)


class AnomalyDetector(KeyedCoProcessFunction):
    """
    Adapter that feeds Flink's keyed MapState/ValueState into spike_detector.evaluate()
    and applies the resulting Decision. See DetectorConfig for the tuning.

    Event time throughout (Plan 06 Phase 2): process_element1 only buckets the
    incoming message and arms a timer; evaluate() itself runs once per
    elapsed event-time second from on_timer, when the watermark -- built
    from sent_at by SentAtTimestampAssigner, below -- passes that second.
    Per-message evaluation depends on message interleaving, so it can't
    replay deterministically; per-second timers can. See
    tools/replay.py for the pure-Python equivalent this mirrors.

    Peak-hold (Plan 06 Phase 3): the detector no longer reports a spike at the
    second that first crosses the trigger. evaluate() opens a hold and records
    the highest intensity while chat stays elevated. It reports one result when
    the period ends or when the cap is reached. That result carries the value
    and the timestamp of the peak. This operator keeps the hold between
    seconds. It also stamps the anomaly with the second of the peak, not the
    second of the report.

    Suppression (Feature 007): the second input carries gift and raid notices,
    which move a per-broadcaster half-open interval and nothing else. It
    registers no timer and emits nothing. The interval is read once, at the
    very end of on_timer, and can only stop the yield -- every count, bucket
    expiry, hold write, chain timer, anomaly counter and last_fire_second write
    above it is identical with the gate on and off, which is what SC-004
    measures.
    """

    def __init__(self, clock_ms=None):
        self.message_counts = None  # MapState: event-time second (sent_at bucket) -> count
        self.hold = None  # ValueState: HoldState as JSON, or null when no episode is open
        self.last_fire_second = None  # ValueState: event-time second of the last emit
        self.suppression = None  # ValueState: SuppressionState as JSON, or null
        self.config = None
        self.suppression_config = None
        self.subtask_index = 0
        # The consumer clock, injected so delivery classification is
        # deterministic under test. None means wall clock at receipt; it is
        # never event time, because the whole point of the measurement is the
        # distance between the two.
        self._clock_ms = clock_ms

    def _consumer_receipt_ms(self) -> int:
        if self._clock_ms is not None:
            return int(self._clock_ms())
        return int(time.time() * 1000)

    def _state_ttl(self):
        """
        TTL for the keyed state of this operator (Plan 06 step 16).

        Which state leaks, exactly: `last_fire_second` is written when the
        detector reports a spike and is never cleared, so one entry stays for
        every broadcaster that ever produced a clip. `hold` stays if a period
        is open when the watermark stops. `message_counts` does not leak --
        on_timer expires its buckets and re-arms the timer only while buckets
        remain, so it empties itself after a broadcaster goes offline. The TTL
        covers all three anyway, because a per-key leak of any size is
        unbounded over the life of the job.

        Size: the TTL must be longer than the span of buckets that one key
        keeps, or the TTL could delete a bucket that the baseline still needs.
        MapState applies the TTL per entry with OnCreateAndWrite, so each
        bucket's clock starts at its single write and must survive
        config.retained_seconds. The factor of 4 gives room for event time to
        run behind processing time, which is what the TTL measures.

        NeverReturnExpired is important for `hold`. If the whole pipeline goes
        quiet with a period open, the watermark stops, no timer runs, and the
        hold stays in state. See the event-time note in
        plans/06-detection-math.md. This setting makes a stale hold read back
        as absent. The detector therefore cannot report a peak from hours ago.
        """
        return (
            StateTtlConfig
            .new_builder(Time.seconds(self.config.retained_seconds * 4))
            .set_update_type(StateTtlConfig.UpdateType.OnCreateAndWrite)
            .set_state_visibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
            # run_cleanup_for_every_record is False. The timer chain already
            # reads all three states once per key per second, and incremental
            # cleanup runs on those accesses. True would add three cleanup
            # scans to every chat message on the hot path instead.
            .cleanup_incrementally(10, False)
            .build()
        )

    def open(self, runtime_context):
        # Start the metrics server here so it comes up on the first chat
        # message this worker processes, not the first anomaly (previously
        # it only started inside the anomaly branch -> the /metrics endpoint
        # stayed dark, tripping ClipDetectorMetricsDown, through any quiet
        # stretch with no spikes -- much more likely now that
        # STD_DEV_THRESHOLD is 4.0 instead of 1.0, and on a scale where a
        # resting channel scores about 0 rather than 7 to 17).
        # Must be here, not at module scope: module-level start_http_server()
        # runs on the jobmanager during job submission too, and pollutes the
        # driver process with an unpicklable thread lock before cloudpickle
        # ships AnomalyDetector() to the task managers (breaks submission
        # entirely: "TypeError: cannot pickle '_thread.lock' object").
        self.subtask_index = runtime_context.get_index_of_this_subtask()
        _init_metrics(self.subtask_index)
        self.config = DetectorConfig.from_env()
        # Built here rather than at module scope for the same reason
        # DetectorConfig is: a bad compose value must stop this operator with a
        # message naming the variable, not silently read as its code default.
        self.suppression_config = SuppressionConfig.from_env()

        ttl_config = self._state_ttl()

        counts_descriptor = MapStateDescriptor("message_counts", Types.LONG(), Types.INT())
        counts_descriptor.enable_time_to_live(ttl_config)
        self.message_counts = runtime_context.get_map_state(counts_descriptor)

        # Flink has no TypeInformation for a dataclass, so the hold travels as
        # JSON -- HoldState.to_json/from_json own the encoding.
        hold_descriptor = ValueStateDescriptor("hold", Types.STRING())
        hold_descriptor.enable_time_to_live(ttl_config)
        self.hold = runtime_context.get_state(hold_descriptor)

        # This state was "last_anomaly_time" and held event-time milliseconds.
        # It now holds event-time seconds, which is what evaluate() accepts.
        # The new name is deliberate. The same name would let Flink restore
        # milliseconds into a field that reads seconds, with no error.
        # Checkpointing is off for this job (see flink-conf.yaml), so Flink
        # restores nothing today.
        last_fire_descriptor = ValueStateDescriptor("last_fire_second", Types.LONG())
        last_fire_descriptor.enable_time_to_live(ttl_config)
        self.last_fire_second = runtime_context.get_state(last_fire_descriptor)

        # Feature 007. JSON in a Types.STRING() ValueState, exactly like the
        # hold, under the same one TTL policy: a deadline that outlived the
        # buckets it gates would be a stale window, and NeverReturnExpired is
        # what makes an expired deadline read back as absent -- which is the
        # same value as not-suppressed, so expiry fails open (FR-011).
        suppression_descriptor = ValueStateDescriptor("suppression", Types.STRING())
        suppression_descriptor.enable_time_to_live(ttl_config)
        self.suppression = runtime_context.get_state(suppression_descriptor)

    def process_element1(self, value, ctx: KeyedCoProcessFunction.Context) -> None:
        try:
            # ctx.timestamp() is sent_at (Twitch's own clock) -- assigned by
            # the post-source SentAtTimestampAssigner before CommandFilter,
            # not our ingestion timestamp and not wall-clock time.
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

    def process_element2(self, value, ctx: KeyedCoProcessFunction.Context) -> None:
        """One `suppression-events` record. Moves a suppression interval and
        nothing else.

        The order is fixed and load-bearing: decode and field types, then the
        fixed future-time trust bound, then and only then delivery observation
        and keyed state (contract section 4.1 rules 1-6, decision 23).

        No timer, no output, no buffering: waiting for suppression before
        deciding is explicitly rejected -- the spec asks for fail-open, not a
        delay (contract section 4.1 rule 7).

        Nothing here can raise. An exception out of this method would fail the
        operator and stop chat detection for every key on the subtask, so a
        rejection is a counted reason instead (contract section 4.1 rule 1).
        """
        broadcaster_id = ctx.get_current_key()
        try:
            # Captured at receipt, before any work: this is the only input to
            # delivery classification, and reading it later would fold this
            # method's own cost into the measurement (research D13).
            consumer_receipt_ms = self._consumer_receipt_ms()

            # The wiring hands the operator the same (key, json) tuple shape
            # the chat side uses. Accepting a bare value as well is defensive:
            # a shape change must not silently stop suppression.
            raw = value
            if isinstance(value, (tuple, list)) and len(value) == 2:
                raw = value[1]

            config = self.suppression_config or SuppressionConfig()
            decoded = decode_suppression_record(raw, config)
            if decoded.notice is None:
                _init_metrics(self.subtask_index)
                if _suppression_records_rejected_total:
                    _suppression_records_rejected_total.labels(
                        reason=decoded.rejected_reason
                    ).inc()
                return

            notice = decoded.notice

            # Future-time trust, before any observation and before any state
            # access (contract section 4.1 rule 4, FR-017, decision 23).
            # apply_notice() only ever moves a deadline outward, so a record
            # claiming to have occurred centuries from now would pin this
            # channel's deadline past every later notice, with no path in the
            # register to move it back. Refusing it here is the only defence,
            # and refusing it BEFORE the delivery signals keeps a rejected
            # record from reading as a healthy consumed one.
            if not is_trustworthy_notice_time(
                notice.occurred_at_ms, consumer_receipt_ms
            ):
                _init_metrics(self.subtask_index)
                if _suppression_records_rejected_total:
                    _suppression_records_rejected_total.labels(
                        reason=SUPPRESSION_REJECT_FIELDS
                    ).inc()
                # Bounded on purpose: the record is by definition untrusted, so
                # the log carries the channel and the arithmetic that refused
                # it and never the payload, its notice_id, or anything that
                # could carry user content (NFR-006).
                logger.warning(
                    f"Suppression notice refused for broadcaster {broadcaster_id}: "
                    f"notice_type={notice.notice_type}, "
                    f"occurred_at_ms={notice.occurred_at_ms} is beyond "
                    f"consumer_receipt_ms={consumer_receipt_ms} plus "
                    f"max_future_skew_seconds={SUPPRESSION_MAX_FUTURE_SKEW_SECONDS}; "
                    f"no deadline written"
                )
                return

            observation = observe_delivery_age(
                notice.occurred_at_ms, consumer_receipt_ms, config
            )
            _init_metrics(self.subtask_index)
            if _suppression_delivery_age_seconds:
                _suppression_delivery_age_seconds.observe(observation.delivery_age_seconds)
            if _suppression_records_consumed_total:
                _suppression_records_consumed_total.labels(
                    lag_class=observation.lag_class
                ).inc()
            if observation.clock_skew:
                # Diagnostic only. The record is still applied: a negative age
                # means the two clocks disagree, not that anything is wrong
                # with the notice itself.
                logger.info(
                    f"Suppression clock skew for broadcaster {broadcaster_id}: "
                    f"notice_type={notice.notice_type}, "
                    f"occurred_at_ms={notice.occurred_at_ms} is ahead of the "
                    f"consumer clock {consumer_receipt_ms}; age clamped to 0"
                )
            if observation.lag_class == SUPPRESSION_LAG_LAGGING:
                # Reported, not dropped -- lateness is not an error here
                # (contract section 4.1 rule 8).
                logger.info(
                    f"Suppression delivery lag for broadcaster {broadcaster_id}: "
                    f"notice_type={notice.notice_type}, "
                    f"delivery_age_seconds={observation.delivery_age_seconds:.3f} "
                    f"above the {config.delivery_lag_warn_seconds}s warning "
                    f"threshold; the notice is still applied"
                )

            # The notice-bounded interval transition. apply_notice returns the
            # SAME object when nothing moved, so this is the write-only-on-
            # change rule the hold already follows: a redelivery or an
            # earlier/equal candidate costs no state write (contract section
            # 4.1 rule 6).
            current = SuppressionState.from_json(self.suppression.value())
            updated = apply_notice(
                current, notice.notice_type, notice.occurred_at_ms, config
            )
            if updated is not current and updated is not None:
                self.suppression.update(updated.to_json())
        except Exception as e:
            logger.error(
                f"Error applying suppression notice for broadcaster {broadcaster_id}: {e}",
                exc_info=True,
            )

    def on_timer(self, timestamp: int, ctx: KeyedCoProcessFunction.OnTimerContext) -> Iterator[str]:
        broadcaster_id = ctx.get_current_key()
        try:
            now_seconds = timestamp // 1000
            # items() reads the map in one pass. keys() plus one get() per key
            # costs one state access per bucket, which is ~305 accesses per
            # broadcaster per second at baseline_seconds=300. It can also
            # return None for a key that keys() just listed, because the TTL
            # filter runs per access. Drop any None for that reason: evaluate()
            # adds these counts and cannot accept one.
            all_counts = {ts: c for ts, c in self.message_counts.items() if c is not None}

            # A message for a second after now_seconds -- up to
            # now_seconds + WATERMARK_OUT_OF_ORDERNESS_SECONDS -- can already be
            # in MapState by the time now_seconds's timer fires. Its own timer
            # only requires the watermark to pass now_seconds, but the
            # watermark itself only advances that far once messages up to
            # ~now_seconds + WATERMARK_OUT_OF_ORDERNESS_SECONDS have already
            # arrived and been counted in process_element1.
            #
            # This filter is defensive, not load-bearing: evaluate() bounds the
            # window with `elif ts_bucket <= second`, so a future bucket falls
            # through every branch and is ignored there too, and its docstring
            # says so. Removing this line does not change any output today.
            # It stays because evaluate()'s docstring also states the opposite
            # direction of the contract -- "the caller must not supply buckets
            # newer than `second`" -- and this is the caller holding up its
            # end. Keep both, or change both together.
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

            # Plan 09 / KNOWN_ISSUES.md Issue 3: this call arrived out of
            # order. It arrived behind hold's own recorded peak. See the
            # guard's comment in spike_detector.py for the full reason.
            # evaluate() passed the hold through, unchanged. So decision.hold
            # equals hold below, and no state write happens. But that also
            # means no ANOMALY DETECTED log fires here. A future
            # investigation into why the cursor regresses would want that
            # evidence. Log and count it instead.
            if decision.hold_regressed:
                logger.warning(
                    f"Hold for broadcaster {broadcaster_id} passed through, unmeasured. "
                    f"This call (second={now_seconds}) arrived behind the hold's own "
                    f"recorded peak (peak_at={hold.peak_at}, {hold.peak_at - now_seconds}s "
                    f"ahead). See KNOWN_ISSUES.md Issue 3 follow-up."
                )
                _init_metrics(self.subtask_index)
                if _hold_regressed_total:
                    _hold_regressed_total.labels(broadcaster_id=str(broadcaster_id)).inc()

            # Keep the elevated period across seconds. Write only when the
            # value changes. The code reads an open hold every second, so an
            # unconditional write would store the same value once per second
            # of the period, up to hold_cap_seconds times.
            if decision.hold != hold:
                if decision.hold is None:
                    self.hold.clear()
                else:
                    self.hold.update(decision.hold.to_json())

            # Keep the per-second cadence going only while this key still has
            # data in its baseline -- an idle broadcaster's chain lapses here
            # and a later message restarts it from process_element1. Checked
            # against all_counts (not counts_as_of_now): a future bucket that
            # was excluded above still needs its own future timer.
            #
            # The cap normally ends an open period long before this chain
            # stops. DetectorConfig keeps hold_cap_seconds below
            # baseline_seconds + window_seconds (25 against 305 at the
            # defaults), and buckets stay for that full span after the last
            # message. One case can still stop the chain with a period open: a
            # watermark that stops, so no timer runs at all. The state TTL
            # above covers it. evaluate() covers the other case itself, by
            # removing a hold whose peak is older than the cap.
            #
            # next_chain_timer(), not timestamp + 1000 directly: KNOWN_ISSUES.md
            # Issue 4 ("Change B") -- registering blindly replayed a whole
            # backlog of already-fired timers once per watermark tick after a
            # jump. See that function's docstring for the full mechanism.
            if all_counts:
                watermark = ctx.timer_service().current_watermark()
                ctx.timer_service().register_event_time_timer(
                    next_chain_timer(timestamp, watermark)
                )

            if decision.emit is not None:
                spike = decision.emit
                # The cooldown starts at the second of the report, which is
                # now. It does not start at the peak, which can be up to
                # hold_cap_seconds earlier.
                self.last_fire_second.update(now_seconds)

                # detected_at is the second of the peak, not this second. Plan
                # 06 Phase 3: the clips table now records the time that chat
                # reached its peak. Before, it recorded the time that the
                # detector first crossed the trigger. Every other field below
                # comes from that same peak second.
                #
                # WARNING: this increases the difference between detected_at
                # and the content of the clip. ClipCreator still asks Twitch
                # for the last 30 seconds at the time that it runs. A period
                # that holds for longer than approximately 20 seconds thus
                # produces a clip that does not contain its own peak. Phase 6
                # (cutover) must correct this. The correction needs the
                # duration and vod_offset data from Plan 04 and is out of scope
                # for this phase. See plans/06-detection-math.md, Out of scope.
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

                # Feature 007, and the only behaviour this feature changes.
                # Everything above ran identically with the gate on and off:
                # the buckets, the expiries, the hold, the chain timer,
                # last_fire_second and anomalies_detected_total. A suppressed
                # decision is therefore state-identical to an emitted one, and
                # "detected but not clipped" stays computable (research D6, R8,
                # SC-004).
                #
                # The compared instant is the PEAK second, not now_seconds, so
                # a burst that peaks inside the interval cannot escape by being
                # reported hold_cap_seconds later (research D5) -- and, in the
                # other direction, a burst that peaked BEFORE the notice stays
                # eligible, because the interval opens at suppress_from_ms
                # (FR-006, FR-007). Absent, expired-under-NeverReturnExpired
                # and unreadable state all read as None here, which is not
                # suppressed: that is the structural form of fail-open
                # (FR-011). There is no retraction path, so a notice that lands
                # after this yield affects only later decisions (FR-018).
                suppression_state = (
                    SuppressionState.from_json(self.suppression.value())
                    if self.suppression is not None
                    else None
                )
                if (
                    self.suppression_config is not None
                    and self.suppression_config.gating_enabled
                    and is_suppressed(suppression_state, spike.detected_at_seconds)
                ):
                    logger.info(
                        f"CLIP SUPPRESSED for broadcaster {broadcaster_id}: "
                        f"notice_type={suppression_state.notice_type}, "
                        f"peak_second={spike.detected_at_seconds}, "
                        f"suppress_from_ms={suppression_state.suppress_from_ms}, "
                        f"suppress_until_ms={suppression_state.suppress_until_ms}, "
                        f"notice_at_ms={suppression_state.notice_at_ms}, "
                        f"intensity={spike.intensity:.2f}"
                    )
                    if _clips_suppressed_total:
                        _clips_suppressed_total.labels(
                            broadcaster_id=str(broadcaster_id),
                            notice_type=suppression_state.notice_type,
                        ).inc()
                    return

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
            # A clip just succeeded, so whatever made us mark this broadcaster
            # clipping-disabled is no longer true (FR-013).
            self.postgres_client.mark_clipping_allowed(broadcaster_id)

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
    # Built here as well as in AnomalyDetector.open(), so a bad compose value
    # stops job submission with a message naming the variable rather than
    # failing every subtask after the cluster has already accepted the job.
    suppression_config = SuppressionConfig.from_env()
    suppression_settings = SuppressionSourceSettings()
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
    logger.info(f"  SUPPRESSION_GATING_ENABLED: {suppression_config.gating_enabled}")
    logger.info(f"  SUPPRESSION_GIFT_WINDOW_SECONDS: {suppression_config.gift_window_seconds}")
    logger.info(f"  SUPPRESSION_RAID_WINDOW_SECONDS: {suppression_config.raid_window_seconds}")
    logger.info(
        f"  SUPPRESSION_DELIVERY_LAG_WARN_SECONDS: "
        f"{suppression_config.delivery_lag_warn_seconds}"
    )
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

    # Build the pipeline. Both sources are entered with no_watermarks() and
    # given their real bounded/idled strategy immediately afterwards, because
    # PyFlink 1.18 executes a Python TimestampAssigner only when it is
    # installed by DataStream.assign_timestamps_and_watermarks() (contract
    # section 1.1.1, decision 25). The sparse second input (Feature 007) is
    # built from SuppressionSourceSettings, which test_spike_detector.py
    # asserts with no PyFlink installed and no broker running (research D16).
    # Both topics are provisioned with one partition per subtask and nothing
    # repartitions between source and assignment, so per-subtask idleness is
    # exactly per-split idleness (docker-compose.yml kafka-init, research
    # section 4.1).
    messages, suppression_records = build_event_time_streams(
        env,
        kafka_source,
        build_suppression_source(suppression_settings),
        suppression_settings,
    )

    # Filter out command messages
    filtered = messages.process(CommandFilter())

    # Key both inputs by broadcaster_id and detect anomalies. The suppression
    # input reaches AnomalyDetector only; CommandFilter and ClipCreator are
    # unchanged and never see it.
    #
    # on_timer yields the anomaly already serialized, so the connected
    # operator's output is the JSON string ClipCreator expects. The unkeying
    # map that used to sit here was a leftover from the keyed-tuple wiring on
    # the input side: it could only ever return its argument unchanged, at the
    # cost of one extra Python operator per record.
    anomalies = connect_detector(filtered, suppression_records, AnomalyDetector())

    # Create clips for detected anomalies
    clips = anomalies.process(ClipCreator())

    # Log created clips
    clips.print()

    # Execute the job
    env.execute("Clip Detector Job")


if __name__ == "__main__":
    main()
