"""Storage interface for the poller's desired chat-subscription set.

The poller publishes ranked broadcaster logins and ids. The reconciler reads
that intent and its generation without knowing the Redis key layout, decoding
rules, or transaction shape.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Mapping

logger = logging.getLogger("stream_monitoring")

DESIRED_KEY = "chat:desired"
DESIRED_IDS_KEY = "chat:desired:ids"
DESIRED_GENERATION_KEY = "chat:desired:generation"


@dataclass(frozen=True)
class DesiredSet:
    """One read of the poller's intent."""

    logins: List[str] = field(default_factory=list)  # rank order, best first
    ids: Dict[str, int] = field(default_factory=dict)
    generation: int = 0

    def broadcaster_ids(self) -> List[int]:
        """Broadcaster ids in rank order, skipping any login with no id."""
        return [self.ids[login] for login in self.logins if login in self.ids]


class DesiredSetStore(ABC):
    """The poller/reconciler seam for ranked subscription intent."""

    @abstractmethod
    def read(self) -> DesiredSet:
        """Return the current desired set and generation."""

    @abstractmethod
    def read_generation(self) -> int:
        """Return only the generation for a cheap stale-view check."""

    @abstractmethod
    def publish(
        self,
        desired: Mapping[str, int],
        broadcaster_ids: Mapping[str, int],
    ) -> None:
        """Replace the desired set and increment its generation atomically."""


class RedisDesiredSetStore(DesiredSetStore):
    """Redis implementation of the desired-set interface.

    The three keys are read synchronously, matching the other Redis access in
    this process. Publishing uses one MULTI/EXEC: readers never see the gap
    between deleting the old members and writing the new set and id map.
    """

    def __init__(self, redis_client):
        self.redis_client = redis_client

    def read(self) -> DesiredSet:
        ranked = self.redis_client.zrange(DESIRED_KEY, 0, -1)
        ids = self.redis_client.hgetall(DESIRED_IDS_KEY) or {}
        parsed: Dict[str, int] = {}
        for login, broadcaster_id in ids.items():
            try:
                parsed[self._as_text(login)] = int(broadcaster_id)
            except (TypeError, ValueError):
                logger.warning(
                    "Unparseable broadcaster id in the desired-set map, skipping it",
                    extra={"login": self._as_text(login)},
                )
        return DesiredSet(
            logins=[self._as_text(login) for login in ranked],
            ids=parsed,
            generation=self.read_generation(),
        )

    def read_generation(self) -> int:
        raw = self.redis_client.get(DESIRED_GENERATION_KEY)
        return int(raw) if raw else 0

    def publish(
        self,
        desired: Mapping[str, int],
        broadcaster_ids: Mapping[str, int],
    ) -> None:
        # DEL is required: a rank trim cannot distinguish a stale member from
        # a wanted member that has the same low score.
        pipe = self.redis_client.pipeline()
        pipe.delete(DESIRED_KEY, DESIRED_IDS_KEY)
        if desired:
            pipe.zadd(DESIRED_KEY, dict(desired))
            pipe.hset(
                DESIRED_IDS_KEY,
                mapping={login: broadcaster_ids[login] for login in desired},
            )
        pipe.incr(DESIRED_GENERATION_KEY)
        pipe.execute()

    @staticmethod
    def _as_text(value) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
