"""Auth and ranking helpers for the spec 004 Phase 5 harnesses.

Vendored from the Phase 0 harnesses rather than imported from them. `phase0/`
is excluded per-clone through `.git/info/exclude`, which is never pushed, so
nothing under it exists in a fresh clone -- importing from there would make
these scripts die at `ModuleNotFoundError` for anyone but the machine they
were written on, which defeats the point of tracking them.

Keep this file dependency-free beyond `twitchAPI` and the standard library.
"""

import json
import os
from pathlib import Path
from typing import List, Tuple

from twitchAPI.twitch import Twitch
from twitchAPI.type import AuthScope

REPO_ROOT = Path(__file__).resolve().parents[3]

# The measurement token, NOT the production one. Same Twitch user (48754970),
# so the create rate limit is shared -- see driver.py's module docstring for
# why that is safe and what it costs.
TOKEN_FILE = Path(
    os.environ.get("PHASE5_TOKEN_FILE", REPO_ROOT / "secrets" / "phase0_tokens.json")
)

SCOPE_MAP = {
    "chat:read": AuthScope.CHAT_READ,
    "clips:edit": AuthScope.CLIPS_EDIT,
    "user:read:chat": AuthScope.USER_READ_CHAT,
}


def load_env(root: Path = REPO_ROOT) -> None:
    """Put TWITCH_CLIENT_ID / _SECRET in the environment if they are not set."""
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


async def make_twitch() -> Tuple[Twitch, str]:
    """Return an authenticated Twitch client and the auth user's id."""
    load_env()
    client_id = os.environ["TWITCH_CLIENT_ID"]
    secret = os.environ["TWITCH_CLIENT_SECRET"]
    record = json.loads(TOKEN_FILE.read_text())
    scopes = [SCOPE_MAP[s] for s in record["scopes"] if s in SCOPE_MAP]

    twitch = await Twitch(client_id, secret)
    await twitch.set_user_authentication(
        record["access_token"], scopes, record["refresh_token"]
    )
    user_id = None
    async for user in twitch.get_users():
        user_id = user.id
        break
    return twitch, user_id


async def top_logins(twitch: Twitch, n: int) -> List[Tuple[str, str]]:
    """The top-n live channels right now as (login, broadcaster_id), by viewers.

    May return fewer than `n` -- Helix pagination runs out somewhere past 1000.
    Callers that depend on the exact count must check it.
    """
    out: List[Tuple[str, str]] = []
    async for stream in twitch.get_streams(first=100):
        out.append((stream.user_login, stream.user_id))
        if len(out) >= n:
            break
    return out
