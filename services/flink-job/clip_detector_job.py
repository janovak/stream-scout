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
FLINK_PARALLELISM = int(os.getenv("FLINK_PARALLELISM", "4"))

# Anomaly detection parameters
WINDOW_SIZE_SECONDS = 5
BASELINE_WINDOW_SECONDS = 300  # 5 minutes of baseline
STD_DEV_THRESHOLD = 3.0
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


class TwitchAPIClient:
    """Client for interacting with Twitch API."""

    def __init__(self, client_id: str, client_secret: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token: Optional[str] = None
        self.token_expires_at: float = 0

    def _ensure_token(self):
        """Ensure we have a valid access token."""
        if self.access_token and time.time() < self.token_expires_at:
            return

        response = requests.post(
            "https://id.twitch.tv/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials"
            }
        )
        response.raise_for_status()
        data = response.json()
        self.access_token = data["access_token"]
        self.token_expires_at = time.time() + data["expires_in"] - 60

    def create_clip(self, broadcaster_id: int) -> Optional[str]:
        """Create a clip for the given broadcaster. Returns clip ID if successful."""
        self._ensure_token()

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id
        }

        response = requests.post(
            "https://api.twitch.tv/helix/clips",
            headers=headers,
            params={"broadcaster_id": str(broadcaster_id)}
        )

        if response.status_code == 202:
            data = response.json()
            if data.get("data"):
                return data["data"][0]["id"]
        return None

    def get_clip(self, clip_id: str) -> Optional[Dict]:
        """Get clip details. Returns clip data if found."""
        self._ensure_token()

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Client-Id": self.client_id
        }

        response = requests.get(
            "https://api.twitch.tv/helix/clips",
            headers=headers,
            params={"id": clip_id}
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("data"):
                return data["data"][0]
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
            self._conn = psycopg2.connect(**self.connection_params)
        return self._conn

    def insert_clip(self, clip: ClipResult):
        """Insert a clip into the database."""
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
                conn.commit()
                logger.info(f"Inserted clip {clip.clip_id} for broadcaster {clip.broadcaster_id}")
        except Exception as e:
            logger.error(f"Failed to insert clip: {e}")
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

    def process_element(self, value: str, ctx: KeyedProcessFunction.Context) -> Iterator[str]:
        try:
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
            if len(counts_baseline) < BASELINE_WINDOW_SECONDS * 0.8:
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
                    logger.info(f"Anomaly detected for broadcaster {broadcaster_id}: "
                               f"count={window_sum}, threshold={threshold:.2f}")
                    yield json.dumps(anomaly)

        except Exception as e:
            logger.error(f"Error in anomaly detection: {e}")


class ClipCreator(ProcessFunction):
    """
    Creates clips via Twitch API when anomalies are detected.
    Implements retry logic with 3 attempts within 10-second window.
    """

    def __init__(self):
        self.twitch_client = None
        self.postgres_client = None

    def open(self, runtime_context):
        self.twitch_client = TwitchAPIClient(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
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

            logger.info(f"Attempting to create clip for broadcaster {broadcaster_id}")

            # Try to create clip with retries
            clip_id = None
            for attempt, delay in enumerate(RETRY_DELAYS):
                if delay > 0:
                    time.sleep(delay)

                try:
                    clip_id = self.twitch_client.create_clip(broadcaster_id)
                    if clip_id:
                        logger.info(f"Clip created: {clip_id} (attempt {attempt + 1})")
                        break
                except Exception as e:
                    logger.warning(f"Clip creation attempt {attempt + 1} failed: {e}")

            if not clip_id:
                logger.error(f"Failed to create clip for broadcaster {broadcaster_id} after {MAX_RETRY_ATTEMPTS} attempts")
                return

            # Wait for clip processing
            time.sleep(15)

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
                self.postgres_client.insert_clip(clip_result)

                yield json.dumps({
                    "broadcaster_id": broadcaster_id,
                    "clip_id": clip_id,
                    "embed_url": clip_result.embed_url,
                    "thumbnail_url": clip_result.thumbnail_url
                })
            else:
                logger.error(f"Failed to retrieve clip metadata for {clip_id}")

        except Exception as e:
            logger.error(f"Error creating clip: {e}")


def main():
    """Main entry point for the Flink job."""
    logger.info("Starting Clip Detector Job")

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
