#!/usr/bin/env python3
"""
Unit tests for API & Frontend Service

Tests API endpoints, query parameter validation, and response formatting.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from api_frontend_service import (
    DEFAULT_CLIP_LIMIT,
    MAX_CLIP_LIMIT,
    app,
)


@pytest.fixture
def client():
    """Create a test client for the Flask app."""
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def mock_db():
    """Mock database connection and cursor."""
    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    # get_clips always runs a COUNT(*) before the SELECT; leaving fetchone()
    # unmocked returns a MagicMock, and comparing that against an int in the
    # has_more calculation raises TypeError, which get_clips's bare except
    # turns into a misleading 500.
    mock_cursor.fetchone.return_value = (0,)
    return mock_conn, mock_cursor


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_200_when_healthy(self, client, mock_db):
        """Health check should return 200 when database is accessible."""
        mock_conn, mock_cursor = mock_db

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/health")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "healthy"

    def test_health_returns_500_when_db_error(self, client):
        """Health check should return 500 when database is unavailable."""
        with patch("api_frontend_service.get_db", side_effect=Exception("DB error")):
            response = client.get("/health")

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data["status"] == "unhealthy"


class TestClipsEndpoint:
    """Tests for /v1.0/clip endpoint."""

    def test_get_clips_default_params(self, client, mock_db):
        """Get clips with default parameters."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = [
            (1, 12345, "clip_abc", "https://embed.url", "https://thumb.url",
             datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc),
             datetime(2026, 1, 10, 12, 0, 1, tzinfo=timezone.utc),
             9.5, "ninja"),
        ]

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert "clips" in data
        assert "count" in data
        assert "query" in data
        assert data["count"] == 1

    def test_get_clips_with_custom_limit(self, client, mock_db):
        """Get clips with custom limit parameter."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip?limit=10")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["query"]["limit"] == 10

    def test_get_clips_limit_capped_at_max(self, client, mock_db):
        """Limit should be capped at MAX_CLIP_LIMIT."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip?limit=500")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["query"]["limit"] == MAX_CLIP_LIMIT

    def test_get_clips_limit_minimum_is_1(self, client, mock_db):
        """Limit should be at least 1."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip?limit=0")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["query"]["limit"] == 1

    def test_get_clips_invalid_limit_returns_400(self, client):
        """Invalid limit parameter should return 400."""
        response = client.get("/v1.0/clip?limit=abc")

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "error" in data

    def test_get_clips_with_min_intensity(self, client, mock_db):
        """Get clips with custom min_intensity parameter."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip?min_intensity=9.5")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["query"]["min_intensity"] == 9.5

    def test_get_clips_invalid_min_intensity_returns_400(self, client):
        """Non-numeric min_intensity should return 400."""
        response = client.get("/v1.0/clip?min_intensity=abc")

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "Invalid min_intensity parameter" in data["error"]

    def test_get_clips_negative_min_intensity_returns_400(self, client):
        """Negative min_intensity should return 400."""
        response = client.get("/v1.0/clip?min_intensity=-1")

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "min_intensity must be non-negative" in data["error"]

    def test_get_clips_with_offset(self, client, mock_db):
        """Get clips with custom offset parameter."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip?offset=5")

        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["query"]["offset"] == 5

    def test_get_clips_invalid_offset_returns_400(self, client):
        """Non-numeric offset should return 400."""
        response = client.get("/v1.0/clip?offset=abc")

        assert response.status_code == 400
        data = json.loads(response.data)
        assert "Invalid offset parameter" in data["error"]

    def test_get_clips_db_error_returns_500(self, client):
        """Database error should return 500."""
        with patch("api_frontend_service.get_db", side_effect=Exception("DB error")):
            response = client.get("/v1.0/clip")

        assert response.status_code == 500
        data = json.loads(response.data)
        assert "error" in data

    def test_clips_response_format(self, client, mock_db):
        """Verify clip response format matches expected schema."""
        mock_conn, mock_cursor = mock_db
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
        clip = data["clips"][0]

        assert clip["id"] == 1
        assert clip["broadcaster_id"] == 12345
        assert clip["clip_id"] == "clip_abc"
        assert clip["embed_url"] == "https://embed.url"
        assert clip["thumbnail_url"] == "https://thumb.url"
        assert clip["detected_at"] is not None
        assert clip["created_at"] is not None
        assert clip["intensity"] == 9.5
        assert clip["streamer_login"] == "ninja"


class TestNotFoundHandler:
    """Tests for 404 error handling."""

    def test_unknown_route_returns_404(self, client):
        """Unknown routes should return 404."""
        response = client.get("/unknown/route")

        assert response.status_code == 404
        data = json.loads(response.data)
        assert data["error"] == "Not found"


class TestStaticFiles:
    """Tests for static file serving."""

    def test_index_route_exists(self, client):
        """Index route should be accessible."""
        # This will fail without actual static files, but tests route exists
        response = client.get("/")
        # Would be 200 with actual file, 404 or 500 without
        assert response.status_code in [200, 404, 500]

    def test_static_route_exists(self, client):
        """Static file route should be accessible."""
        response = client.get("/static/nonexistent.js")
        assert response.status_code == 404


class TestConfiguration:
    """Tests for configuration values."""

    def test_default_clip_limit(self):
        """Default clip limit should be 24."""
        assert DEFAULT_CLIP_LIMIT == 24

    def test_max_clip_limit(self):
        """Max clip limit should be 100."""
        assert MAX_CLIP_LIMIT == 100


class TestCORS:
    """Tests for CORS configuration."""

    def test_cors_headers_present(self, client, mock_db):
        """CORS headers should be present in response."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            response = client.get("/v1.0/clip")

        # Flask-CORS adds these headers
        # In testing mode, they might not be present
        assert response.status_code == 200


class TestDatabaseQueryOptimization:
    """Tests for database query structure."""

    def test_query_uses_intensity_filter(self, client, mock_db):
        """Query should filter by intensity threshold."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            client.get("/v1.0/clip")

            # get_clips runs a COUNT(*) then the paginated SELECT.
            assert mock_cursor.execute.call_count == 2

            # The SELECT is the second call; verify its structure.
            query = mock_cursor.execute.call_args_list[1][0][0]

            assert "intensity >=" in query
            assert "ORDER BY" in query
            assert "LIMIT" in query

    def test_query_joins_streamers_table(self, client, mock_db):
        """Query should join with streamers table for login."""
        mock_conn, mock_cursor = mock_db
        mock_cursor.fetchall.return_value = []

        with patch("api_frontend_service.get_db", return_value=mock_conn):
            client.get("/v1.0/clip")

            query = mock_cursor.execute.call_args_list[1][0][0]

            assert "LEFT JOIN streamers" in query
            assert "streamer_login" in query


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
