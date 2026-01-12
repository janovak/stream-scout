#!/usr/bin/env python3
"""
API & Frontend Service

Serves REST API for clip queries and static frontend files.
Uses direct Postgres connection with connection pooling.
"""

import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Optional

import psycopg2
import psycopg2.pool
from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS
from pythonjsonlogger import jsonlogger

# Configuration
POSTGRES_URL = os.getenv("POSTGRES_URL", "postgresql://twitch:twitch_password@localhost:5432/twitch")
HTTP_PORT = int(os.getenv("HTTP_PORT", "5000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
DEFAULT_CLIP_LIMIT = 50
MAX_CLIP_LIMIT = 100
DEFAULT_DAYS_BACK = 7

# Logging setup
logger = logging.getLogger("api_frontend")
logger.setLevel(getattr(logging, LOG_LEVEL.upper()))
handler = logging.StreamHandler(sys.stdout)
formatter = jsonlogger.JsonFormatter(
    fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
handler.setFormatter(formatter)
logger.addHandler(handler)

# Flask app
app = Flask(__name__, static_folder=STATIC_DIR)
CORS(app)

# Database connection pool
db_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def get_db_pool():
    """Get or create the database connection pool."""
    global db_pool
    if db_pool is None:
        db_pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=POSTGRES_URL
        )
        logger.info("Database connection pool initialized")
    return db_pool


def get_db():
    """Get a database connection from the pool."""
    if "db" not in g:
        pool = get_db_pool()
        g.db = pool.getconn()
    return g.db


@app.teardown_appcontext
def close_db(error):
    """Return the database connection to the pool."""
    db = g.pop("db", None)
    if db is not None and db_pool is not None:
        db_pool.putconn(db)


def log_request(f):
    """Decorator to log request and response details."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        start_time = datetime.now(timezone.utc)
        response = f(*args, **kwargs)
        duration_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

        logger.info("Request processed", extra={
            "method": request.method,
            "path": request.path,
            "status": response.status_code if hasattr(response, "status_code") else 200,
            "duration_ms": round(duration_ms, 2),
            "query_params": dict(request.args)
        })

        return response
    return decorated_function


def parse_iso8601(date_string: str) -> Optional[datetime]:
    """Parse ISO 8601 datetime string."""
    if not date_string:
        return None

    try:
        # Handle various ISO 8601 formats
        if date_string.endswith("Z"):
            date_string = date_string[:-1] + "+00:00"
        return datetime.fromisoformat(date_string)
    except ValueError:
        return None


@app.route("/")
@log_request
def index():
    """Serve the frontend HTML."""
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
@log_request
def static_files(filename):
    """Serve static assets."""
    return send_from_directory(STATIC_DIR, filename)


@app.route("/health")
def health():
    """Health check endpoint."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
        return jsonify({"status": "healthy"}), 200
    except Exception as e:
        logger.error("Health check failed", extra={"error": str(e)})
        return jsonify({"status": "unhealthy", "error": str(e)}), 500


@app.route("/v1.0/clip")
@log_request
def get_clips():
    """
    Query clips with optional time range and pagination.

    Query Parameters:
    - start: ISO 8601 timestamp for start of range (default: 7 days ago)
    - end: ISO 8601 timestamp for end of range (default: now)
    - limit: Maximum number of clips to return (default: 50, max: 100)
    """
    try:
        # Parse query parameters
        start_str = request.args.get("start")
        end_str = request.args.get("end")
        limit_str = request.args.get("limit")

        # Parse and validate limit
        try:
            limit = int(limit_str) if limit_str else DEFAULT_CLIP_LIMIT
            limit = min(max(1, limit), MAX_CLIP_LIMIT)
        except ValueError:
            return jsonify({"error": "Invalid limit parameter"}), 400

        # Parse and validate time range
        now = datetime.now(timezone.utc)
        default_start = now - timedelta(days=DEFAULT_DAYS_BACK)

        start_time = parse_iso8601(start_str) if start_str else default_start
        end_time = parse_iso8601(end_str) if end_str else now

        if start_str and start_time is None:
            return jsonify({"error": "Invalid start timestamp format. Use ISO 8601."}), 400
        if end_str and end_time is None:
            return jsonify({"error": "Invalid end timestamp format. Use ISO 8601."}), 400

        if start_time > end_time:
            return jsonify({"error": "Start time must be before end time"}), 400

        # Query database
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    c.id,
                    c.broadcaster_id,
                    c.clip_id,
                    c.embed_url,
                    c.thumbnail_url,
                    c.detected_at,
                    c.created_at,
                    s.streamer_login
                FROM clips c
                LEFT JOIN streamers s ON c.broadcaster_id = s.streamer_id
                WHERE c.detected_at >= %s AND c.detected_at <= %s
                ORDER BY c.detected_at DESC
                LIMIT %s
            """, (start_time, end_time, limit))

            rows = cur.fetchall()

        # Format response
        clips = []
        for row in rows:
            clips.append({
                "id": row[0],
                "broadcaster_id": row[1],
                "clip_id": row[2],
                "embed_url": row[3],
                "thumbnail_url": row[4],
                "detected_at": row[5].isoformat() if row[5] else None,
                "created_at": row[6].isoformat() if row[6] else None,
                "streamer_login": row[7]
            })

        return jsonify({
            "clips": clips,
            "count": len(clips),
            "query": {
                "start": start_time.isoformat(),
                "end": end_time.isoformat(),
                "limit": limit
            }
        })

    except Exception as e:
        logger.error("Error fetching clips", extra={"error": str(e)})
        return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(500)
def server_error(error):
    """Handle 500 errors."""
    logger.error("Internal server error", extra={"error": str(error)})
    return jsonify({"error": "Internal server error"}), 500


def main():
    """Main entry point."""
    init_db_pool()
    logger.info("Starting API & Frontend Service", extra={"port": HTTP_PORT})
    app.run(host="0.0.0.0", port=HTTP_PORT, debug=False)


if __name__ == "__main__":
    main()
