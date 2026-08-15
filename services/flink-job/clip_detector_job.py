#!/usr/bin/env python3
"""
PyFlink Clip Detector Job

Consumes chat messages from Kafka, detects anomalies using sliding windows,
creates clips via Twitch API, and stores metadata in Postgres.
"""

import json
import logging
import os
import re
import time
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
from pyflink.common.time import Duration
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
from pyflink.datastream.state import MapStateDescriptor, ValueStateDescriptor

from spike_detector import DetectorConfig, evaluate
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

# Clip creation retry parameters
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAYS = [0, 2, 4]  # seconds (within 5-second retry window)
CLIP_DELAY_SECONDS = 10  # Wait before first clip attempt to center moment in clip

# Twitch's own docs (dev.twitch.tv/docs/api/clips/) only say clip creation is
# async and to "assume it failed" if Get Clips hasn't returned the clip after
# 15 seconds -- no recommended poll interval or attempt count, and no stated
# guarantee of a minimum or maximum processing time either way. A single
# check at the 15s mark (the old behavior) missed ~15% of real clips per our
# own metrics, so we retry with real backoff instead of waiting once and
# giving up. Tried pushing this much further out (t=5,60,360,1260) to see if
# the small residual failure rate (~1-1.5% even with the original schedule)
# was just slow clips needing more time -- it wasn't: zero recoveries on the
# added attempts 3/4 across everything we watched, same failure rate as
# before, just taking up to 21 minutes to find out instead of 50 seconds.
# Settled on a modest bump over the original instead: same 4 attempts, same
# shape, just a longer last step (t=5,15,30,60 vs the original t=5,15,30,50).
GET_CLIP_MAX_ATTEMPTS = 4
GET_CLIP_RETRY_DELAYS = [5, 10, 15, 30]  # seconds before each attempt (t=5,15,30,60)

# Command message regex
COMMAND_PATTERN = re.compile(r"^![a-zA-Z0-9]+")

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
                    INSERT INTO clips (broadcaster_id, clip_id, embed_url, thumbnail_url, detected_at, intensity)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (clip_id) DO NOTHING
                """, (
                    clip.broadcaster_id,
                    clip.clip_id,
                    clip.embed_url,
                    clip.thumbnail_url,
                    datetime.fromtimestamp(clip.detected_at / 1000, tz=timezone.utc),
                    clip.intensity
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
            if not COMMAND_PATTERN.match(text):
                yield value
        except json.JSONDecodeError:
            pass


class AnomalyDetector(KeyedProcessFunction):
    """
    Adapter that feeds Flink's keyed MapState/ValueState into spike_detector.evaluate()
    and applies the resulting Decision. See DetectorConfig for the tuning.
    """

    def __init__(self):
        self.message_counts = None  # MapState: timestamp_bucket -> count
        self.last_anomaly_time = None  # ValueState: last anomaly timestamp
        self.config = None
        self.subtask_index = 0

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
        self.message_counts = runtime_context.get_map_state(
            MapStateDescriptor("message_counts", Types.LONG(), Types.INT())
        )
        self.last_anomaly_time = runtime_context.get_state(
            ValueStateDescriptor("last_anomaly_time", Types.LONG())
        )
        self.config = DetectorConfig.from_env()

    def process_element(self, value, ctx: KeyedProcessFunction.Context) -> Iterator[str]:
        broadcaster_id = None
        try:
            # value is a tuple (broadcaster_id, json_string) from the key_by operation
            if isinstance(value, tuple):
                broadcaster_id, json_str = value
                msg = json.loads(json_str)
            else:
                msg = json.loads(value)
                broadcaster_id = msg["broadcaster_id"]
            timestamp = msg["timestamp"]

            # Calculate time bucket (1-second buckets)
            bucket = timestamp // 1000

            # Update message count for this bucket
            current_count = self.message_counts.get(bucket)
            if current_count is None:
                current_count = 0
            self.message_counts.put(bucket, current_count + 1)

            counts = {ts: self.message_counts.get(ts) for ts in self.message_counts.keys()}
            current_time = int(time.time())
            decision = evaluate(counts, current_time, self.last_anomaly_time.value(), self.config)

            for expired_bucket in decision.expired_buckets:
                self.message_counts.remove(expired_bucket)

            if decision.spike:
                spike = decision.spike
                current_ms = current_time * 1000
                self.last_anomaly_time.update(current_ms)

                anomaly = {
                    "broadcaster_id": broadcaster_id,
                    "detected_at": current_ms,
                    "message_count": spike.message_count,
                    "baseline_mean": spike.baseline_mean,
                    "baseline_std": spike.baseline_std,
                    "intensity": spike.intensity
                }
                threshold = spike.baseline_mean + (self.config.std_dev_threshold * spike.baseline_std)
                logger.info(f"ANOMALY DETECTED for broadcaster {broadcaster_id}: "
                           f"count={spike.message_count}, threshold={threshold:.2f}, mean={spike.baseline_mean:.2f}, "
                           f"std={spike.baseline_std:.2f}, intensity={spike.intensity:.2f}")
                _init_metrics(self.subtask_index)
                if _anomalies_detected_total:
                    _anomalies_detected_total.labels(broadcaster_id=str(broadcaster_id)).inc()
                yield json.dumps(anomaly)

        except Exception as e:
            broadcaster_str = str(broadcaster_id) if broadcaster_id else "unknown"
            logger.error(f"Error in anomaly detection for broadcaster {broadcaster_str}: {e}", exc_info=True)


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
        # The single Postgres connection and the lazily-initialized Prometheus
        # metrics can't handle concurrent use from multiple clip threads --
        # these serialize just the quick DB write / first-init check, not the
        # long sleeps around them.
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
        # (CLIP_DELAY_SECONDS + retry delays + GET_CLIP_RETRY_DELAYS) waiting
        # on Twitch. Running it inline on process_element would block this
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
            start_time = time.time()

            logger.info(f"=== CLIP CREATION START for broadcaster {broadcaster_id} ===")
            logger.info(f"Anomaly details: count={anomaly.get('message_count')}, "
                       f"mean={anomaly.get('baseline_mean', 0):.2f}, std={anomaly.get('baseline_std', 0):.2f}")

            # Wait before first clip attempt to center the moment in the clip
            logger.info(f"Waiting {CLIP_DELAY_SECONDS}s before clip creation to center moment in clip...")
            time.sleep(CLIP_DELAY_SECONDS)

            # Try to create clip with smart retries (only retry transient errors)
            clip_id = None
            last_error = None
            for attempt, delay in enumerate(RETRY_DELAYS):
                if delay > 0:
                    logger.info(f"Retry delay: waiting {delay}s before attempt {attempt + 1}")
                    time.sleep(delay)

                try:
                    logger.info(f"Clip creation attempt {attempt + 1}/{MAX_RETRY_ATTEMPTS}")
                    clip_id = self.twitch_client.create_clip(broadcaster_id)
                    if clip_id:
                        logger.info(f"Clip creation successful on attempt {attempt + 1}: clip_id={clip_id}")
                        break
                    else:
                        logger.warning(f"Clip creation attempt {attempt + 1} returned no clip_id")
                except TwitchAPIError as e:
                    last_error = e
                    logger.warning(f"Clip creation attempt {attempt + 1} failed: {e} (retryable={e.is_retryable})")
                    if not e.is_retryable:
                        logger.error(f"Non-retryable error (status={e.status_code}), stopping retry attempts")
                        break
                except Exception as e:
                    last_error = e
                    logger.warning(f"Clip creation attempt {attempt + 1} unexpected exception: {e}")
                    # Unexpected errors are not retryable
                    break

            if not clip_id:
                with self._metrics_lock:
                    _init_metrics(self.subtask_index)
                if last_error:
                    logger.error(f"CLIP CREATION FAILED for broadcaster {broadcaster_id}: {last_error}")
                    reason = "api_error" if isinstance(last_error, TwitchAPIError) else "unknown"
                    if _clips_created_failed_total:
                        _clips_created_failed_total.labels(broadcaster_id=str(broadcaster_id), reason=reason).inc()
                    # Twitch returns 403 here specifically when the broadcaster hasn't
                    # authorized clip creation on their channel -- that's permanent
                    # until they change it, not something a retry or token refresh
                    # fixes. Record it so stream-monitoring stops watching their chat.
                    if isinstance(last_error, TwitchAPIError) and last_error.status_code == 403:
                        logger.warning(f"Broadcaster {broadcaster_id} does not authorize clip creation; marking allows_clipping=FALSE")
                        with self._postgres_lock:
                            self.postgres_client.mark_clipping_disabled(broadcaster_id)
                else:
                    logger.error(f"CLIP CREATION FAILED for broadcaster {broadcaster_id} after {MAX_RETRY_ATTEMPTS} attempts")
                    if _clips_created_failed_total:
                        _clips_created_failed_total.labels(broadcaster_id=str(broadcaster_id), reason="max_retries").inc()
                return

            # Poll for clip metadata -- Twitch's clip processing is async and
            # doesn't always finish by the time a single check would land, so
            # retry a few times instead of waiting once and giving up.
            clip_data = None
            for attempt, delay in enumerate(GET_CLIP_RETRY_DELAYS):
                logger.info(f"Waiting {delay}s before clip metadata attempt {attempt + 1}/{GET_CLIP_MAX_ATTEMPTS} for {clip_id}...")
                time.sleep(delay)
                clip_data = self.twitch_client.get_clip(clip_id)
                if clip_data:
                    logger.info(f"Clip metadata retrieved on attempt {attempt + 1}: clip_id={clip_id}")
                    break
                # Not ready yet is expected mid-retry, not a problem -- only the
                # exhaustion after the final attempt (logged below, once the loop
                # ends without a break) is an actual failure.
                logger.info(f"Clip metadata attempt {attempt + 1}/{GET_CLIP_MAX_ATTEMPTS} found nothing yet for {clip_id}")

            if clip_data:
                # Get intensity from anomaly data
                intensity = anomaly.get("intensity")

                clip_result = ClipResult(
                    broadcaster_id=broadcaster_id,
                    clip_id=clip_id,
                    embed_url=clip_data.get("embed_url", ""),
                    thumbnail_url=clip_data.get("thumbnail_url", ""),
                    detected_at=detected_at,
                    success=True,
                    intensity=intensity
                )

                # Store in Postgres
                logger.info(f"Storing clip {clip_id} in database...")
                with self._postgres_lock:
                    self.postgres_client.insert_clip(clip_result)

                # Record success metrics
                duration = time.time() - start_time
                with self._metrics_lock:
                    _init_metrics(self.subtask_index)
                if _clips_created_success_total:
                    _clips_created_success_total.labels(broadcaster_id=str(broadcaster_id)).inc()
                if _clip_creation_duration_seconds:
                    _clip_creation_duration_seconds.labels(broadcaster_id=str(broadcaster_id)).set(duration)

                # Nothing downstream consumes ClipCreator's old yielded output
                # besides clips.print() (a debug echo) -- this log line is the
                # durable record, alongside the Postgres row and metrics above.
                logger.info(f"=== CLIP CREATION COMPLETE for broadcaster {broadcaster_id}: clip_id={clip_id} (took {duration:.1f}s) ===")
            else:
                logger.error(f"CLIP METADATA RETRIEVAL FAILED for clip_id={clip_id} after {GET_CLIP_MAX_ATTEMPTS} attempts")
                with self._metrics_lock:
                    _init_metrics(self.subtask_index)
                if _clips_created_failed_total:
                    _clips_created_failed_total.labels(broadcaster_id=str(broadcaster_id), reason="metadata_fetch").inc()

        except Exception as e:
            logger.error(f"CLIP CREATION ERROR for broadcaster {broadcaster_id}: {e}", exc_info=True)


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
    logger.info(f"  DETECTION_STD_DEV_THRESHOLD: {detector_config.std_dev_threshold}")
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

    # Create watermark strategy
    watermark_strategy = WatermarkStrategy \
        .for_bounded_out_of_orderness(Duration.of_seconds(5)) \
        .with_idleness(Duration.of_minutes(1))

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
