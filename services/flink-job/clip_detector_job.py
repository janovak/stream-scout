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
import statistics
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterator, Optional, Tuple

import requests
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

# Anomaly detection parameters
WINDOW_SIZE_SECONDS = 5
BASELINE_WINDOW_SECONDS = 300  # 5 minutes of baseline
STD_DEV_THRESHOLD = 1.0
COOLDOWN_SECONDS = 30
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAYS = [0, 3, 6]  # seconds

# Command message regex
COMMAND_PATTERN = re.compile(r"^![a-zA-Z0-9]+")

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("clip_detector")


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


class TwitchAPIError(Exception):
    """Custom exception for Twitch API errors."""
    def __init__(self, message: str, status_code: int, is_retryable: bool):
        super().__init__(message)
        self.status_code = status_code
        self.is_retryable = is_retryable


class TwitchAPIClient:
    """Client for interacting with Twitch API using user OAuth tokens."""

    def __init__(self, client_id: str, client_secret: str, token_file: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file = token_file
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._load_tokens()

    def _load_tokens(self):
        """Load user tokens from JSON file."""
        try:
            with open(self.token_file, "r") as f:
                data = json.load(f)
            self.access_token = data.get("access_token")
            self.refresh_token = data.get("refresh_token")
            scopes = data.get("scopes", [])
            logger.info(f"Loaded user tokens from {self.token_file}, scopes={scopes}")
            if not self.access_token or not self.refresh_token:
                raise ValueError("Token file missing access_token or refresh_token")
        except FileNotFoundError:
            logger.error(f"Token file not found: {self.token_file}")
            raise
        except Exception as e:
            logger.error(f"Failed to load tokens: {e}")
            raise

    def _save_tokens(self):
        """Save tokens back to file after refresh."""
        try:
            # Read existing data to preserve scopes
            with open(self.token_file, "r") as f:
                data = json.load(f)
            data["access_token"] = self.access_token
            data["refresh_token"] = self.refresh_token
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            with open(self.token_file, "w") as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved refreshed tokens to {self.token_file}")
        except Exception as e:
            logger.error(f"Failed to save tokens: {e}")

    def _refresh_access_token(self):
        """Refresh the access token using the refresh token."""
        logger.info("Refreshing user access token...")
        try:
            response = requests.post(
                "https://id.twitch.tv/oauth2/token",
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token
                },
                timeout=30
            )
            if response.status_code != 200:
                logger.error(f"Token refresh failed: status={response.status_code}, body={response.text}")
                response.raise_for_status()
            data = response.json()
            self.access_token = data["access_token"]
            self.refresh_token = data.get("refresh_token", self.refresh_token)
            logger.info(f"Token refreshed successfully, expires in {data.get('expires_in', 'unknown')}s")
            self._save_tokens()
        except Exception as e:
            logger.error(f"Token refresh exception: {e}")
            raise

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
                self._refresh_access_token()
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
                    logger.warning(f"Get clip returned 200 but no data for clip_id={clip_id}")
            elif response.status_code == 401:
                # Token expired - refresh and retry
                logger.warning("Got 401 on get_clip, attempting token refresh...")
                self._refresh_access_token()
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
        logger.info(f"Inserting clip into database: clip_id={clip.clip_id}, broadcaster_id={clip.broadcaster_id}")
        conn = self._get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO clips (broadcaster_id, clip_id, embed_url, thumbnail_url, detected_at)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (clip_id) DO NOTHING
                """, (
                    clip.broadcaster_id,
                    clip.clip_id,
                    clip.embed_url,
                    clip.thumbnail_url,
                    datetime.fromtimestamp(clip.detected_at / 1000, tz=timezone.utc)
                ))
                rows_affected = cur.rowcount
                conn.commit()
                if rows_affected > 0:
                    logger.info(f"Successfully inserted clip {clip.clip_id} for broadcaster {clip.broadcaster_id}")
                else:
                    logger.warning(f"Clip {clip.clip_id} already exists (conflict), no insert performed")
        except Exception as e:
            logger.error(f"Failed to insert clip {clip.clip_id}: {e}")
            conn.rollback()
            raise

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
    Detects anomalies in chat message frequency using sliding windows.
    Uses mean + 3 standard deviations as threshold.
    Implements 30-second cooldown between detections per broadcaster.
    """

    def __init__(self):
        self.message_counts = None  # MapState: timestamp_bucket -> count
        self.last_anomaly_time = None  # ValueState: last anomaly timestamp
        self.baseline_stats = None  # ValueState: (mean, std_dev, sample_count)

    def open(self, runtime_context):
        self.message_counts = runtime_context.get_map_state(
            MapStateDescriptor("message_counts", Types.LONG(), Types.INT())
        )
        self.last_anomaly_time = runtime_context.get_state(
            ValueStateDescriptor("last_anomaly_time", Types.LONG())
        )
        self.baseline_stats = runtime_context.get_state(
            ValueStateDescriptor("baseline_stats", Types.TUPLE([Types.FLOAT(), Types.FLOAT(), Types.INT()]))
        )

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

            # Clean up old buckets and calculate statistics
            current_time = int(time.time())
            baseline_start = current_time - BASELINE_WINDOW_SECONDS
            window_start = current_time - WINDOW_SIZE_SECONDS

            counts_baseline = []
            counts_window = []

            # Iterate through all buckets
            for ts_bucket in list(self.message_counts.keys()):
                count = self.message_counts.get(ts_bucket)
                if ts_bucket < baseline_start:
                    # Remove old data
                    self.message_counts.remove(ts_bucket)
                elif ts_bucket >= baseline_start:
                    counts_baseline.append(count)
                    if ts_bucket >= window_start:
                        counts_window.append(count)

            # Need enough data for baseline (at least 5 minutes worth)
            min_required = int(BASELINE_WINDOW_SECONDS * 0.8)
            if len(counts_baseline) < min_required:
                # Log periodically (every ~60 seconds worth of messages)
                if len(counts_baseline) % 60 == 0 and len(counts_baseline) > 0:
                    logger.debug(f"Broadcaster {broadcaster_id}: insufficient baseline data "
                                f"({len(counts_baseline)}/{min_required} buckets)")
                return

            # Calculate baseline statistics
            if len(counts_baseline) >= 2:
                mean = statistics.mean(counts_baseline)
                std_dev = statistics.stdev(counts_baseline)
            else:
                return

            # Calculate current window activity
            window_sum = sum(counts_window) if counts_window else 0

            # Check for anomaly
            threshold = mean + (STD_DEV_THRESHOLD * std_dev)
            if window_sum > threshold and std_dev > 0:
                # Check cooldown
                last_anomaly = self.last_anomaly_time.value()
                current_ms = current_time * 1000

                if last_anomaly is None or (current_ms - last_anomaly) > (COOLDOWN_SECONDS * 1000):
                    # Anomaly detected!
                    self.last_anomaly_time.update(current_ms)

                    anomaly = {
                        "broadcaster_id": broadcaster_id,
                        "detected_at": current_ms,
                        "message_count": window_sum,
                        "baseline_mean": mean,
                        "baseline_std": std_dev
                    }
                    logger.info(f"ANOMALY DETECTED for broadcaster {broadcaster_id}: "
                               f"count={window_sum}, threshold={threshold:.2f}, mean={mean:.2f}, std={std_dev:.2f}")
                    yield json.dumps(anomaly)
                else:
                    cooldown_remaining = (COOLDOWN_SECONDS * 1000) - (current_ms - last_anomaly)
                    logger.debug(f"Anomaly blocked by cooldown for broadcaster {broadcaster_id}: "
                                f"count={window_sum}, cooldown_remaining={cooldown_remaining/1000:.1f}s")

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

    def open(self, runtime_context):
        self.twitch_client = TwitchAPIClient(
            TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET, TWITCH_TOKEN_FILE
        )
        self.postgres_client = PostgresClient(
            POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB,
            POSTGRES_USER, POSTGRES_PASSWORD
        )

    def close(self):
        if self.postgres_client:
            self.postgres_client.close()

    def process_element(self, value: str, ctx: ProcessFunction.Context) -> Iterator[str]:
        try:
            anomaly = json.loads(value)
            broadcaster_id = anomaly["broadcaster_id"]
            detected_at = anomaly["detected_at"]

            logger.info(f"=== CLIP CREATION START for broadcaster {broadcaster_id} ===")
            logger.info(f"Anomaly details: count={anomaly.get('message_count')}, "
                       f"mean={anomaly.get('baseline_mean', 0):.2f}, std={anomaly.get('baseline_std', 0):.2f}")

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
                if last_error:
                    logger.error(f"CLIP CREATION FAILED for broadcaster {broadcaster_id}: {last_error}")
                else:
                    logger.error(f"CLIP CREATION FAILED for broadcaster {broadcaster_id} after {MAX_RETRY_ATTEMPTS} attempts")
                return

            # Wait for clip processing
            logger.info(f"Waiting 15s for Twitch to process clip {clip_id}...")
            time.sleep(15)
            logger.info(f"Wait complete, fetching clip metadata for {clip_id}")

            # Get clip metadata
            clip_data = self.twitch_client.get_clip(clip_id)
            if clip_data:
                clip_result = ClipResult(
                    broadcaster_id=broadcaster_id,
                    clip_id=clip_id,
                    embed_url=clip_data.get("embed_url", ""),
                    thumbnail_url=clip_data.get("thumbnail_url", ""),
                    detected_at=detected_at,
                    success=True
                )

                # Store in Postgres
                logger.info(f"Storing clip {clip_id} in database...")
                self.postgres_client.insert_clip(clip_result)

                logger.info(f"=== CLIP CREATION COMPLETE for broadcaster {broadcaster_id}: clip_id={clip_id} ===")
                yield json.dumps({
                    "broadcaster_id": broadcaster_id,
                    "clip_id": clip_id,
                    "embed_url": clip_result.embed_url,
                    "thumbnail_url": clip_result.thumbnail_url
                })
            else:
                logger.error(f"CLIP METADATA RETRIEVAL FAILED for clip_id={clip_id}")

        except Exception as e:
            logger.error(f"CLIP CREATION ERROR for value={value[:200]}: {e}", exc_info=True)


def main():
    """Main entry point for the Flink job."""
    logger.info("=" * 60)
    logger.info("Starting Clip Detector Job")
    logger.info("=" * 60)
    logger.info(f"Configuration:")
    logger.info(f"  KAFKA_BOOTSTRAP_SERVERS: {KAFKA_BOOTSTRAP_SERVERS}")
    logger.info(f"  POSTGRES_HOST: {POSTGRES_HOST}")
    logger.info(f"  FLINK_PARALLELISM: {FLINK_PARALLELISM}")
    logger.info(f"  WINDOW_SIZE_SECONDS: {WINDOW_SIZE_SECONDS}")
    logger.info(f"  BASELINE_WINDOW_SECONDS: {BASELINE_WINDOW_SECONDS}")
    logger.info(f"  STD_DEV_THRESHOLD: {STD_DEV_THRESHOLD}")
    logger.info(f"  COOLDOWN_SECONDS: {COOLDOWN_SECONDS}")
    logger.info(f"  TWITCH_CLIENT_ID: {'set' if TWITCH_CLIENT_ID else 'NOT SET'}")
    logger.info(f"  TWITCH_CLIENT_SECRET: {'set' if TWITCH_CLIENT_SECRET else 'NOT SET'}")
    logger.info(f"  TWITCH_TOKEN_FILE: {TWITCH_TOKEN_FILE}")
    logger.info(f"  RETRYABLE_STATUS_CODES: {RETRYABLE_STATUS_CODES}")

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
