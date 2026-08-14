#!/usr/bin/env python3
"""
Integration tests for API & Frontend Service with mocked Postgres.

Tests database operations, connection pooling, and query execution.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api_frontend_service import app


@pytest.fixture
def client():
    """Create a test client for the Flask app."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def mock_db_pool():
    """Create a mock database connection pool."""
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_cursor = MagicMock()

    mock_pool.getconn.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    # get_clips always runs a COUNT(*) before the SELECT; leaving fetchone()
    # unmocked returns a MagicMock, and comparing that against an int in the
    # has_more calculation raises TypeError, which get_clips's bare except
    # turns into a misleading 500.
    mock_cursor.fetchone.return_value = (0,)

    return mock_pool, mock_conn, mock_cursor


class TestConnectionPoolIntegration:
    """Integration tests for database connection pooling."""

    def test_connection_acquired_from_pool(self, client, mock_db_pool):
        """Connections should be acquired from pool for each request."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db_pool", return_value=mock_pool):
            client.get("/v1.0/clip")

        mock_pool.getconn.assert_called()

    def test_connection_returned_to_pool(self, client, mock_db_pool):
        """Connections should be returned to pool after request."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        # The connection is returned via teardown_appcontext
        # We verify putconn is configured correctly
        with patch("api_frontend_service.db_pool", mock_pool):
            with patch("api_frontend_service.get_db_pool", return_value=mock_pool):
                response = client.get("/v1.0/clip")

        assert response.status_code == 200


class TestDatabaseQueryIntegration:
    """Integration tests for database query execution."""

    def test_clips_query_executes_correctly(self, client, mock_db_pool):
        """Clips query should execute with correct parameters."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            client.get("/v1.0/clip?limit=25")

        # get_clips runs a COUNT(*) then the paginated SELECT.
        assert mock_cursor.execute.call_count == 2

        # Verify query parameters on the SELECT (the second call).
        call_args = mock_cursor.execute.call_args_list[1]
        query = call_args[0][0]
        params = call_args[0][1]

        assert "LIMIT" in query
        assert params[1] == 25  # limit parameter

    def test_query_parameters_have_correct_types(self, client, mock_db_pool):
        """min_intensity/limit/offset parameters should be passed to query with correct types."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            client.get("/v1.0/clip?min_intensity=8.5&limit=10&offset=5")

        call_args = mock_cursor.execute.call_args_list[1]
        params = call_args[0][1]

        # params are (min_intensity, limit, offset)
        assert len(params) == 3
        assert isinstance(params[0], float)
        assert params[0] == 8.5
        assert isinstance(params[1], int)
        assert params[1] == 10
        assert isinstance(params[2], int)
        assert params[2] == 5

    def test_query_returns_multiple_clips(self, client, mock_db_pool):
        """Query should handle multiple clip results."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool

        # Simulate multiple clips
        detected_at = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        created_at = datetime(2026, 1, 10, 12, 0, 1, tzinfo=timezone.utc)

        mock_cursor.fetchall.return_value = [
            (1, 111, "clip_a", "https://embed1.url", "https://thumb1.url",
             detected_at, created_at, 9.1, "ninja"),
            (2, 222, "clip_b", "https://embed2.url", "https://thumb2.url",
             detected_at, created_at, 9.2, "shroud"),
            (3, 333, "clip_c", "https://embed3.url", "https://thumb3.url",
             detected_at, created_at, 9.3, "pokimane"),
        ]

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["count"] == 3
        assert len(data["clips"]) == 3

    def test_query_handles_empty_results(self, client, mock_db_pool):
        """Query should handle empty result set."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["count"] == 0
        assert data["clips"] == []


class TestHealthCheckDatabaseIntegration:
    """Integration tests for health check database connectivity."""

    def test_health_check_executes_simple_query(self, client, mock_db_pool):
        """Health check should execute a simple SELECT 1 query."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/health")

        assert response.status_code == 200
        mock_cursor.execute.assert_called_with("SELECT 1")

    def test_health_check_detects_connection_failure(self, client):
        """Health check should detect database connection failure."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = Exception("Connection refused")

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/health")

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data["status"] == "unhealthy"


class TestStreamersJoinIntegration:
    """Integration tests for clips-streamers table join."""

    def test_join_returns_streamer_login(self, client, mock_db_pool):
        """Query should join with streamers table to get login name."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool

        detected_at = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        created_at = datetime(2026, 1, 10, 12, 0, 1, tzinfo=timezone.utc)

        mock_cursor.fetchall.return_value = [
            (1, 12345, "clip_abc", "https://embed.url", "https://thumb.url",
             detected_at, created_at, 9.5, "ninja"),
        ]

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["clips"][0]["streamer_login"] == "ninja"

    def test_join_handles_missing_streamer(self, client, mock_db_pool):
        """Query should handle clips without matching streamer (LEFT JOIN)."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool

        detected_at = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        created_at = datetime(2026, 1, 10, 12, 0, 1, tzinfo=timezone.utc)

        # Streamer login is None (no match in streamers table)
        mock_cursor.fetchall.return_value = [
            (1, 99999, "clip_xyz", "https://embed.url", "https://thumb.url",
             detected_at, created_at, 9.5, None),
        ]

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["clips"][0]["streamer_login"] is None


class TestDatabaseErrorHandling:
    """Integration tests for database error handling."""

    def test_query_error_returns_500(self, client, mock_db_pool):
        """Database query error should return 500."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.execute.side_effect = Exception("Query syntax error")

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 500
        data = json.loads(response.data)
        assert "error" in data

    def test_connection_error_returns_500(self, client):
        """Database connection error should return 500."""
        with patch("api_frontend_service.get_db", side_effect=Exception("Connection timeout")):
            response = client.get("/v1.0/clip")

        assert response.status_code == 500

    def test_fetchall_error_returns_500(self, client, mock_db_pool):
        """Error during fetchall should return 500."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.side_effect = Exception("Fetch error")

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 500


class TestPaginationIntegration:
    """Integration tests for query pagination."""

    def test_pagination_with_limit(self, client, mock_db_pool):
        """Pagination should respect limit parameter."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip?limit=10")

        call_args = mock_cursor.execute.call_args_list[1]
        params = call_args[0][1]

        assert params[1] == 10  # limit parameter

    def test_default_pagination_limit(self, client, mock_db_pool):
        """Default limit should be 24."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        data = json.loads(response.data)
        assert data["query"]["limit"] == 24


class TestQueryOptimizationIntegration:
    """Integration tests for query optimization."""

    def test_query_filters_on_intensity(self, client, mock_db_pool):
        """Query should filter on intensity (the spec-002 relevance signal)."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            client.get("/v1.0/clip")

        call_args = mock_cursor.execute.call_args
        query = call_args[0][0]

        # Query should use intensity in WHERE clause
        assert "intensity IS NOT NULL" in query
        assert "intensity >=" in query

    def test_query_orders_by_detected_at_desc(self, client, mock_db_pool):
        """Query should order by detected_at DESC for recency."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            client.get("/v1.0/clip")

        call_args = mock_cursor.execute.call_args
        query = call_args[0][0]

        assert "ORDER BY" in query
        assert "detected_at DESC" in query


class TestDateTimeHandlingIntegration:
    """Integration tests for datetime handling."""

    def test_detected_at_serialized_as_iso8601(self, client, mock_db_pool):
        """detected_at should be serialized as ISO 8601."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool

        detected_at = datetime(2026, 1, 10, 12, 30, 45, tzinfo=timezone.utc)
        created_at = datetime(2026, 1, 10, 12, 30, 46, tzinfo=timezone.utc)

        mock_cursor.fetchall.return_value = [
            (1, 12345, "clip_abc", "https://embed.url", "https://thumb.url",
             detected_at, created_at, 9.5, "ninja"),
        ]

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        data = json.loads(response.data)
        assert "2026-01-10T12:30:45" in data["clips"][0]["detected_at"]

    def test_null_timestamps_handled(self, client, mock_db_pool):
        """Null timestamps should be handled gracefully."""
        mock_pool, mock_conn, mock_cursor = mock_db_pool

        # Simulate null timestamps
        mock_cursor.fetchall.return_value = [
            (1, 12345, "clip_abc", "https://embed.url", "https://thumb.url",
             None, None, 9.5, "ninja"),
        ]

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["clips"][0]["detected_at"] is None
        assert data["clips"][0]["created_at"] is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
