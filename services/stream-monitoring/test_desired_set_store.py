"""Tests for the desired-set storage interface and Redis implementation."""

import logging

import pytest
from redis.exceptions import ResponseError

from desired_set_store import (
    DESIRED_GENERATION_KEY,
    DESIRED_IDS_KEY,
    DESIRED_KEY,
    DesiredSet,
    RedisDesiredSetStore,
)
from test_support import FakeRedis


class TestFakeRedisBatchContract:
    def test_mget_returns_ordered_values_and_none_entries(self):
        fake_redis = FakeRedis()
        fake_redis.strings.update({"first": "1", "third": "3"})

        assert fake_redis.mget(["first", "second", "third"]) == [
            "1",
            None,
            "3",
        ]

    def test_transactional_and_non_transactional_pipelines_return_ordered_results(self):
        fake_redis = FakeRedis()

        desired = fake_redis.pipeline()
        desired.incr("generation")
        assert desired.transaction is True
        assert desired.execute() == [1]

        refresh = fake_redis.pipeline(transaction=False)
        refresh.setex("streamer:online:first", 180, 101)
        refresh.setex("streamer:online:second", 180, 202)
        assert refresh.transaction is False
        assert refresh.execute(raise_on_error=True) == [True, True]
        assert fake_redis.strings["streamer:online:first"] == "101"
        assert fake_redis.strings["streamer:online:second"] == "202"

    def test_element_error_applies_other_commands_and_raises_first_error(self):
        fake_redis = FakeRedis()
        first_error = ResponseError("first rejected command")
        later_error = ResponseError("later rejected command")
        fake_redis.inject_pipeline_response_error(
            "online_refresh", 1, first_error
        )
        fake_redis.inject_pipeline_response_error(
            "online_refresh", 2, later_error
        )
        pipeline = fake_redis.pipeline(transaction=False)
        pipeline.setex("streamer:online:first", 180, 101)
        pipeline.setex("streamer:online:second", 180, 202)
        pipeline.setex("streamer:online:third", 180, 303)

        with pytest.raises(ResponseError, match="first rejected command"):
            pipeline.execute(raise_on_error=True)

        assert fake_redis.strings["streamer:online:first"] == "101"
        assert "streamer:online:second" not in fake_redis.strings
        assert "streamer:online:third" not in fake_redis.strings
        assert fake_redis.last_pipeline_responses == [
            True,
            first_error,
            later_error,
        ]

    def test_transport_failure_before_application_differs_from_ack_loss(self):
        fake_redis = FakeRedis()
        fake_redis.inject_pipeline_failure("online_refresh", when="before")
        pipeline = fake_redis.pipeline(transaction=False)
        pipeline.setex("streamer:online:before", 180, 101)

        with pytest.raises(ConnectionError, match="before application"):
            pipeline.execute(raise_on_error=True)

        assert "streamer:online:before" not in fake_redis.strings

        fake_redis.inject_pipeline_failure("online_refresh", when="after")
        pipeline = fake_redis.pipeline(transaction=False)
        pipeline.setex("streamer:online:after", 180, 202)

        with pytest.raises(ConnectionError, match="acknowledgement lost"):
            pipeline.execute(raise_on_error=True)

        assert fake_redis.strings["streamer:online:after"] == "202"

    def test_dispatch_recording_counts_batches_not_queued_commands(self):
        fake_redis = FakeRedis()

        fake_redis.mget(["streamer:online:first", "streamer:online:second"])
        refresh = fake_redis.pipeline(transaction=False)
        refresh.setex("streamer:online:first", 180, 101)
        refresh.setex("streamer:online:second", 180, 202)
        refresh.execute()
        desired = fake_redis.pipeline()
        desired.delete("chat:desired")
        desired.incr("chat:desired:generation")
        desired.execute()

        assert fake_redis.calls == [
            "mget",
            "pipeline.execute",
            "pipeline.execute",
        ]
        assert fake_redis.dispatches == [
            {
                "phase": "online_snapshot",
                "kind": "command",
                "operation": "mget",
            },
            {
                "phase": "online_refresh",
                "kind": "pipeline",
                "operation": "execute",
                "transaction": False,
            },
            {
                "phase": "desired_set_publication",
                "kind": "pipeline",
                "operation": "execute",
                "transaction": True,
            },
        ]


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
