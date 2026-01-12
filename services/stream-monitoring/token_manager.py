"""
Twitch OAuth Token Manager

Handles loading, saving, and refreshing OAuth tokens for headless environments.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("token_manager")

# Default token file path (can be overridden via environment variable)
DEFAULT_TOKEN_FILE = "/app/secrets/twitch_user_tokens.json"
TOKEN_FILE_PATH = Path(os.getenv("TWITCH_TOKEN_FILE", DEFAULT_TOKEN_FILE))


class TokenManager:
    """Manages Twitch OAuth tokens with file-based persistence."""

    def __init__(self, token_file: Optional[Path] = None):
        """
        Initialize the token manager.

        Args:
            token_file: Path to the token JSON file. Defaults to TOKEN_FILE_PATH.
        """
        self.token_file = token_file or TOKEN_FILE_PATH
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._scopes: list[str] = []

    def load_tokens(self) -> Tuple[Optional[str], Optional[str], list[str]]:
        """
        Load tokens from the JSON file.

        Returns:
            Tuple of (access_token, refresh_token, scopes)

        Raises:
            FileNotFoundError: If token file doesn't exist
            json.JSONDecodeError: If token file is invalid JSON
        """
        if not self.token_file.exists():
            raise FileNotFoundError(
                f"Token file not found: {self.token_file}\n"
                "Run 'python seed_twitch_tokens.py' to generate tokens."
            )

        with open(self.token_file, "r") as f:
            data = json.load(f)

        self._access_token = data.get("access_token")
        self._refresh_token = data.get("refresh_token")
        self._scopes = data.get("scopes", [])

        if not self._access_token or not self._refresh_token:
            raise ValueError("Token file is missing access_token or refresh_token")

        logger.info("Loaded tokens from file", extra={
            "token_file": str(self.token_file),
            "scopes": self._scopes,
            "has_access_token": bool(self._access_token),
            "has_refresh_token": bool(self._refresh_token)
        })

        return self._access_token, self._refresh_token, self._scopes

    def save_tokens(self, access_token: str, refresh_token: str) -> None:
        """
        Save tokens to the JSON file.

        Args:
            access_token: The new access token
            refresh_token: The new refresh token
        """
        # Ensure directory exists
        self.token_file.parent.mkdir(parents=True, exist_ok=True)

        # Update internal state
        self._access_token = access_token
        self._refresh_token = refresh_token

        # Save to file
        token_data = {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "scopes": self._scopes,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        with open(self.token_file, "w") as f:
            json.dump(token_data, f, indent=2)

        logger.info("Saved refreshed tokens to file", extra={
            "token_file": str(self.token_file)
        })

    @property
    def access_token(self) -> Optional[str]:
        """Get the current access token."""
        return self._access_token

    @property
    def refresh_token(self) -> Optional[str]:
        """Get the current refresh token."""
        return self._refresh_token

    @property
    def scopes(self) -> list[str]:
        """Get the token scopes."""
        return self._scopes


# Global token manager instance
_token_manager: Optional[TokenManager] = None


def get_token_manager() -> TokenManager:
    """Get or create the global token manager instance."""
    global _token_manager
    if _token_manager is None:
        _token_manager = TokenManager()
    return _token_manager


def load_tokens() -> Tuple[Optional[str], Optional[str], list[str]]:
    """Load tokens using the global token manager."""
    return get_token_manager().load_tokens()


def save_tokens(access_token: str, refresh_token: str) -> None:
    """Save tokens using the global token manager."""
    get_token_manager().save_tokens(access_token, refresh_token)
