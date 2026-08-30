"""Tests for the desired-set storage interface and Redis implementation."""

import logging

from desired_set_store import (
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
    DesiredSet,
    RedisDesiredSetStore,
)
from test_support import FakeRedis


class TestRedisDesiredSetStore:
    def test_publish_and_read_round_trip_in_rank_order(self):
        fake_redis = FakeRedis()
        store = RedisDesiredSetStore(fake_redis)

        store.publish(
            {"second": 2, "first": 1},
            {"first": 101, "second": 202},
        )

        assert store.read() == DesiredSet(
            logins=["first", "second"],
            ids={"first": 101, "second": 202},
            generation=1,
        )

    def test_publish_replaces_both_keys_and_increments_generation(self):
        fake_redis = FakeRedis()
        store = RedisDesiredSetStore(fake_redis)
        store.publish({"old": 1}, {"old": 101})

        store.publish({"new": 1}, {"new": 202})

        assert store.read() == DesiredSet(
            logins=["new"],
            ids={"new": 202},
            generation=2,
        )

    def test_empty_publish_clears_the_set_in_one_transaction(self):
        fake_redis = FakeRedis()
        store = RedisDesiredSetStore(fake_redis)
        store.publish({"old": 1}, {"old": 101})
        fake_redis.calls.clear()

        store.publish({}, {})

        assert fake_redis.calls == ["pipeline.execute"]
        assert store.read() == DesiredSet(generation=2)

    def test_read_decodes_bytes_and_skips_only_the_bad_id(self, caplog):
        fake_redis = FakeRedis()
        fake_redis.zsets[DESIRED_KEY] = {b"good": 1.0, b"bad": 2.0}
        fake_redis.hashes[DESIRED_IDS_KEY] = {
            b"good": b"101",
            b"bad": b"not-an-id",
        }
        fake_redis.strings[DESIRED_GENERATION_KEY] = b"4"

        with caplog.at_level(logging.WARNING):
            desired = RedisDesiredSetStore(fake_redis).read()

        assert desired == DesiredSet(
            logins=["good", "bad"],
            ids={"good": 101},
            generation=4,
        )
        assert "Unparseable broadcaster id" in caplog.text
