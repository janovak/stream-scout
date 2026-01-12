#!/usr/bin/env python3
"""
Stream Monitoring Service

Monitors top Twitch streams, joins chat rooms, and publishes messages to Kafka.
Uses Redis for online streamer state management with TTL-based expiration.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional, Set

import psycopg2
import psycopg2.pool
import redis
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from confluent_kafka import Producer
from prometheus_client import Counter, Gauge, start_http_server
from pythonjsonlogger import jsonlogger
from twitchAPI.chat import Chat, ChatMessage, EventData
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope, ChatEvent

from token_manager import TokenManager, get_token_manager


# Configuration
KAFKA_BROKER_URL = os.getenv("KAFKA_BROKER_URL", "localhost:9092")
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://twitch:twitch_password@localhost:5432/twitch")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "9100"))
HEALTH_CHECK_PORT = int(os.getenv("HEALTH_CHECK_PORT", "8080"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Hysteresis thresholds for chat room management
# Join chat when streamer enters top JOIN_THRESHOLD
# Leave chat only when streamer exits top LEAVE_THRESHOLD
# This preserves Flink baseline data during rank fluctuations
JOIN_THRESHOLD = 5   # Join chat rooms for top 5 streamers
LEAVE_THRESHOLD = 10  # Only leave when streamer drops out of top 10

REDIS_STREAMER_TTL = 180  # 3 minutes TTL for streamer online status
POLL_INTERVAL_SECONDS = 120  # Poll every 2 minutes

# Logging setup
logger = logging.getLogger("stream_monitoring")
logger.setLevel(getattr(logging, LOG_LEVEL.upper()))
handler = logging.StreamHandler(sys.stdout)
formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# Prometheus metrics
active_stream_count = Gauge("active_stream_count", "Number of currently monitored streams")
chat_messages_total = Counter("chat_messages_total", "Total chat messages processed", ["broadcaster_id"])
twitch_api_errors_total = Counter("twitch_api_errors_total", "Total Twitch API errors", ["error_type"])
kafka_messages_produced = Counter("kafka_messages_produced", "Total Kafka messages produced", ["topic"])


class StreamMonitoringService:
    """Main service class for monitoring Twitch streams."""

    def __init__(self):
        self.twitch: Optional[Twitch] = None
        self.chat: Optional[Chat] = None
        self.scheduler: Optional[AsyncIOScheduler] = None
        self.kafka_producer: Optional[Producer] = None
        self.db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
        self.redis_client: Optional[redis.Redis] = None
        self.token_manager: Optional[TokenManager] = None
        self.running = True
        self.joined_channels: Set[str] = set()
        self.broadcaster_ids: Dict[str, int] = {}  # login -> id mapping

    async def _on_token_refresh(self, access_token: str, refresh_token: str):
        """Callback invoked when tokens are refreshed by pyTwitchAPI."""
        logger.info("Twitch tokens refreshed, persisting to file")
        if self.token_manager:
            self.token_manager.save_tokens(access_token, refresh_token)

    async def initialize(self):
        """Initialize all connections and services."""
        logger.info("Initializing Stream Monitoring Service")

        # Initialize Twitch API with app credentials
        self.twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)

        # Load user tokens from file and set up user authentication
        self.token_manager = get_token_manager()
        try:
            access_token, refresh_token, scopes = self.token_manager.load_tokens()

            # Convert scope strings to AuthScope enums
            auth_scopes = []
            for scope in scopes:
                if scope == "chat:read":
                    auth_scopes.append(AuthScope.CHAT_READ)
                elif scope == "clips:edit":
                    auth_scopes.append(AuthScope.CLIPS_EDIT)

            # Set user authentication with loaded tokens
            await self.twitch.set_user_authentication(
                access_token,
                auth_scopes,
                refresh_token
            )

            # Register callback for token refresh
            self.twitch.user_auth_refresh_callback = self._on_token_refresh

            logger.info("User authentication configured with pre-seeded tokens", extra={
                "scopes": scopes
            })

        except FileNotFoundError as e:
            logger.warning("Token file not found, running without user auth (chat will not work)", extra={
                "error": str(e)
            })
            # Fall back to app-only auth for streams API
            await self.twitch.authenticate_app([])

        # Initialize Kafka producer
        self.kafka_producer = Producer({
            "bootstrap.servers": KAFKA_BROKER_URL,
            "client.id": "stream-monitoring-service",
            "acks": "all",
            "retries": 3,
            "retry.backoff.ms": 1000,
        })
        logger.info("Kafka producer initialized", extra={"broker": KAFKA_BROKER_URL})

        # Initialize Postgres connection pool
        self.db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=POSTGRES_URL
        )
        logger.info("Postgres connection pool initialized")

        # Initialize Redis
        self.redis_client = redis.from_url(REDIS_URL, decode_responses=True)
        self.redis_client.ping()
        logger.info("Redis connection initialized")

        # Initialize scheduler
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self.poll_top_streams,
            "interval",
            seconds=POLL_INTERVAL_SECONDS,
            id="poll_streams",
            next_run_time=datetime.now(timezone.utc)
        )

        # Start Prometheus metrics server
        start_http_server(PROMETHEUS_PORT)
        logger.info("Prometheus metrics server started", extra={"port": PROMETHEUS_PORT})

    async def start(self):
        """Start the service."""
        await self.initialize()
        self.scheduler.start()
        logger.info("Stream Monitoring Service started")

        # Keep the service running
        while self.running:
            await asyncio.sleep(1)

    async def stop(self):
        """Gracefully stop the service."""
        logger.info("Stopping Stream Monitoring Service")
        self.running = False

        if self.scheduler:
            self.scheduler.shutdown(wait=True)

        if self.chat is not None:
            try:
                await self.chat.stop()
            except Exception as e:
                logger.warning("Error stopping chat client", extra={"error": str(e)})

        if self.twitch:
            await self.twitch.close()

        if self.kafka_producer:
            self.kafka_producer.flush(timeout=10)

        if self.db_pool:
            self.db_pool.closeall()

        if self.redis_client:
            self.redis_client.close()

        logger.info("Stream Monitoring Service stopped")

    async def poll_top_streams(self):
        """Poll Twitch API for top streams and manage chat connections."""
        try:
            logger.info("Polling for top streams")

            # Get top LEAVE_THRESHOLD streams (we need to know who's in top 10 for hysteresis)
            streams = []
            async for stream in self.twitch.get_streams(first=LEAVE_THRESHOLD):
                streams.append(stream)
                if len(streams) >= LEAVE_THRESHOLD:
                    break

            # Track streamers by rank for hysteresis logic
            top_join_streamers = set()  # Streamers in top JOIN_THRESHOLD (eligible to join)
            top_leave_streamers = set()  # Streamers in top LEAVE_THRESHOLD (not yet eligible to leave)

            for rank, stream in enumerate(streams, 1):
                broadcaster_login = stream.user_login.lower()
                broadcaster_id = int(stream.user_id)
                self.broadcaster_ids[broadcaster_login] = broadcaster_id

                # Track which threshold each streamer is in
                if rank <= JOIN_THRESHOLD:
                    top_join_streamers.add(broadcaster_login)
                if rank <= LEAVE_THRESHOLD:
                    top_leave_streamers.add(broadcaster_login)

                # Update Redis with TTL
                redis_key = f"streamer:online:{broadcaster_login}"
                is_new = not self.redis_client.exists(redis_key)
                self.redis_client.setex(redis_key, REDIS_STREAMER_TTL, broadcaster_id)

                # Update Postgres
                self._upsert_streamer(broadcaster_id, broadcaster_login)

                # Publish lifecycle event if new (only for top JOIN_THRESHOLD)
                if is_new and rank <= JOIN_THRESHOLD:
                    self._publish_lifecycle_event("online", broadcaster_id, broadcaster_login, rank)
                    logger.info("Streamer online", extra={
                        "broadcaster_login": broadcaster_login,
                        "broadcaster_id": broadcaster_id,
                        "rank": rank
                    })

            # Check for streamers that went offline (dropped out of top LEAVE_THRESHOLD)
            for login in list(self.joined_channels):
                if login not in top_leave_streamers:
                    redis_key = f"streamer:online:{login}"
                    if not self.redis_client.exists(redis_key):
                        broadcaster_id = self.broadcaster_ids.get(login, 0)
                        self._publish_lifecycle_event("offline", broadcaster_id, login, 0)
                        logger.info("Streamer offline", extra={
                            "broadcaster_login": login,
                            "broadcaster_id": broadcaster_id
                        })

            # Update metrics (count of streamers we're actively monitoring)
            active_stream_count.set(len(self.joined_channels))

            # Manage chat connections with hysteresis
            await self._manage_chat_connections(top_join_streamers, top_leave_streamers)

        except Exception as e:
            logger.error("Error polling streams", extra={"error": str(e)})
            twitch_api_errors_total.labels(error_type="poll_streams").inc()

    async def _manage_chat_connections(self, join_eligible: Set[str], leave_eligible: Set[str]):
        """
        Manage chat room connections with hysteresis.

        Args:
            join_eligible: Streamers in top JOIN_THRESHOLD (should join if not already joined)
            leave_eligible: Streamers in top LEAVE_THRESHOLD (should NOT leave yet)

        Hysteresis logic:
        - Join chat when streamer enters top 5 (JOIN_THRESHOLD)
        - Leave chat only when streamer drops out of top 10 (LEAVE_THRESHOLD)
        - This prevents thrashing and preserves Flink baseline data
        """
        # Join channels for streamers who entered top JOIN_THRESHOLD
        channels_to_join = join_eligible - self.joined_channels

        # Only leave channels for streamers who dropped out of top LEAVE_THRESHOLD
        # (i.e., they're currently joined but NOT in leave_eligible set)
        channels_to_leave = self.joined_channels - leave_eligible

        # Initialize chat if needed
        if channels_to_join and not self.chat:
            try:
                self.chat = await Chat(self.twitch)
                self.chat.register_event(ChatEvent.READY, self._on_chat_ready)
                self.chat.register_event(ChatEvent.MESSAGE, self._on_chat_message)
                self.chat.start()
                logger.info("Chat client started")
            except Exception as e:
                logger.error("Failed to start chat client", extra={"error": str(e)})
                return

        # Join new channels (entered top JOIN_THRESHOLD)
        for channel in channels_to_join:
            try:
                await self.chat.join_room(channel)
                self.joined_channels.add(channel)
                logger.info("Joined chat room", extra={
                    "channel": channel,
                    "reason": f"entered top {JOIN_THRESHOLD}"
                })
            except Exception as e:
                logger.error("Failed to join chat room", extra={"channel": channel, "error": str(e)})

        # Leave channels (dropped out of top LEAVE_THRESHOLD)
        for channel in channels_to_leave:
            try:
                await self.chat.leave_room(channel)
                self.joined_channels.discard(channel)
                logger.info("Left chat room", extra={
                    "channel": channel,
                    "reason": f"exited top {LEAVE_THRESHOLD}"
                })
            except Exception as e:
                logger.error("Failed to leave chat room", extra={"channel": channel, "error": str(e)})

    async def _on_chat_ready(self, ready_event: EventData):
        """Handle chat ready event."""
        logger.info("Chat client ready")

    async def _on_chat_message(self, msg: ChatMessage):
        """Handle incoming chat messages."""
        try:
            broadcaster_login = msg.room.name.lower()
            broadcaster_id = self.broadcaster_ids.get(broadcaster_login)

            if not broadcaster_id:
                return

            # Build message payload
            message_payload = {
                "broadcaster_id": broadcaster_id,
                "timestamp": int(time.time() * 1000),
                "message_id": str(uuid.uuid4()),
                "text": msg.text,
                "user_id": int(msg.user.id) if msg.user.id else 0,
                "user_name": msg.user.name,
                "metadata": {
                    "emotes": {},
                    "badges": dict(msg.user.badges) if msg.user.badges else {},
                    "is_subscriber": msg.user.subscriber,
                    "is_mod": msg.user.mod
                }
            }

            # Publish to Kafka
            self._publish_chat_message(broadcaster_id, message_payload)

            # Update metrics
            chat_messages_total.labels(broadcaster_id=str(broadcaster_id)).inc()

        except Exception as e:
            logger.error("Error processing chat message", extra={"error": str(e)})

    def _publish_chat_message(self, broadcaster_id: int, message: dict):
        """Publish chat message to Kafka."""
        try:
            self.kafka_producer.produce(
                topic="chat-messages",
                key=str(broadcaster_id).encode("utf-8"),
                value=json.dumps(message).encode("utf-8"),
                callback=self._delivery_callback
            )
            self.kafka_producer.poll(0)
            kafka_messages_produced.labels(topic="chat-messages").inc()
        except Exception as e:
            logger.error("Failed to publish chat message", extra={"error": str(e)})

    def _publish_lifecycle_event(self, event_type: str, broadcaster_id: int,
                                  broadcaster_login: str, rank: int):
        """Publish stream lifecycle event to Kafka."""
        try:
            event = {
                "event_type": event_type,
                "broadcaster_id": broadcaster_id,
                "broadcaster_login": broadcaster_login,
                "rank": rank,
                "timestamp": int(time.time())
            }
            self.kafka_producer.produce(
                topic="stream-lifecycle",
                key=str(broadcaster_id).encode("utf-8"),
                value=json.dumps(event).encode("utf-8"),
                callback=self._delivery_callback
            )
            self.kafka_producer.poll(0)
            kafka_messages_produced.labels(topic="stream-lifecycle").inc()
        except Exception as e:
            logger.error("Failed to publish lifecycle event", extra={"error": str(e)})

    def _delivery_callback(self, err, msg):
        """Kafka delivery callback."""
        if err:
            logger.error("Kafka delivery failed", extra={
                "error": str(err),
                "topic": msg.topic()
            })
        else:
            logger.debug("Kafka message delivered", extra={
                "topic": msg.topic(),
                "partition": msg.partition()
            })

    def _upsert_streamer(self, streamer_id: int, streamer_login: str):
        """Insert or update streamer in Postgres."""
        conn = None
        try:
            conn = self.db_pool.getconn()
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO streamers (streamer_id, streamer_login, last_seen_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (streamer_id) DO UPDATE
                    SET streamer_login = EXCLUDED.streamer_login,
                        last_seen_at = NOW()
                """, (streamer_id, streamer_login))
                conn.commit()
        except Exception as e:
            logger.error("Failed to upsert streamer", extra={
                "streamer_id": streamer_id,
                "error": str(e)
            })
            if conn:
                conn.rollback()
        finally:
            if conn:
                self.db_pool.putconn(conn)


async def run_health_check_server():
    """Run a simple HTTP health check server."""
    from aiohttp import web

    async def health_handler(request):
        return web.Response(text="OK", status=200)

    app = web.Application()
    app.router.add_get("/health", health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HEALTH_CHECK_PORT)
    await site.start()
    logger.info("Health check server started", extra={"port": HEALTH_CHECK_PORT})


async def main():
    """Main entry point."""
    service = StreamMonitoringService()

    # Set up signal handlers
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        asyncio.create_task(service.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Start health check server
    await run_health_check_server()

    # Start main service
    try:
        await service.start()
    except Exception as e:
        logger.error("Service error", extra={"error": str(e)})
        await service.stop()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
