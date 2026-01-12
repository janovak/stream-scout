#!/usr/bin/env python3
"""
Twitch OAuth Token Seeding Tool

One-time CLI tool for generating OAuth tokens in headless environments.
Uses pyTwitchAPI's CodeFlow for device code authentication.

Usage:
    python seed_twitch_tokens.py

The tool will:
1. Generate an authorization URL
2. Print the URL for you to visit in a browser
3. Wait for you to complete authentication on Twitch
4. Save the tokens to secrets/twitch_user_tokens.json
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from twitchAPI.oauth import CodeFlow
from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope

# Configuration
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID", "")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET", "")
TOKEN_FILE_PATH = Path(__file__).parent / "secrets" / "twitch_user_tokens.json"

# Required scopes for chat reading and clip creation
REQUIRED_SCOPES = [AuthScope.CHAT_READ, AuthScope.CLIPS_EDIT]


def save_tokens(access_token: str, refresh_token: str, scopes: list[str]) -> None:
    """Save tokens to JSON file."""
    TOKEN_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)

    token_data = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scopes": scopes,
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    with open(TOKEN_FILE_PATH, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"\nTokens saved to: {TOKEN_FILE_PATH}")


async def seed_tokens() -> None:
    """Run the token seeding flow."""
    # Validate credentials
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        print("Error: TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables are required.")
        print("\nSet them using:")
        print("  export TWITCH_CLIENT_ID=your_client_id")
        print("  export TWITCH_CLIENT_SECRET=your_client_secret")
        sys.exit(1)

    print("=" * 60, flush=True)
    print("Twitch OAuth Token Seeding Tool", flush=True)
    print("=" * 60, flush=True)
    print(f"\nClient ID: {TWITCH_CLIENT_ID[:8]}...", flush=True)
    print(f"Required scopes: {', '.join(s.value for s in REQUIRED_SCOPES)}", flush=True)
    print(flush=True)

    # Initialize Twitch client
    twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)

    # Create CodeFlow for headless authentication
    code_flow = CodeFlow(twitch, REQUIRED_SCOPES)

    # Get authorization code and URL
    code, url = await code_flow.get_code()

    print("=" * 60, flush=True)
    print("AUTHORIZATION REQUIRED", flush=True)
    print("=" * 60, flush=True)
    print(f"\n1. Open this URL in your browser:\n", flush=True)
    print(f"   {url}", flush=True)
    print(f"\n2. Log in with your Twitch account", flush=True)
    print(f"3. Authorize the application", flush=True)
    print(f"\nWaiting for authorization to complete...", flush=True)
    print("(Press Ctrl+C to cancel)", flush=True)
    print(flush=True)

    try:
        # Wait for user to complete authorization
        access_token, refresh_token = await code_flow.wait_for_auth_complete()

        print("\n" + "=" * 60)
        print("AUTHORIZATION SUCCESSFUL!")
        print("=" * 60)

        # Save tokens
        scope_strings = [s.value for s in REQUIRED_SCOPES]
        save_tokens(access_token, refresh_token, scope_strings)

        print("\nYou can now start the Stream Monitoring Service.")
        print("The tokens will be automatically refreshed when they expire.")

    except asyncio.CancelledError:
        print("\n\nAuthorization cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nAuthorization failed: {e}")
        sys.exit(1)
    finally:
        await twitch.close()


def main():
    """Main entry point."""
    try:
        asyncio.run(seed_tokens())
    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
