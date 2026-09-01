"""Shared in-memory adapters for stream-monitoring unit tests."""

from redis.exceptions import ResponseError

_NO_OVERRIDE = object()


class FakeRedis:
    """Enough Redis for the poller/reconciler seam, held in memory."""

    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.hashes = {}
        self.calls = []
        self.dispatches = []
        self.mget_requests = []
        self.mget_response_override = _NO_OVERRIDE
        self.pipeline_executions = []
        self.fail_on = set()
        self._recording = True
        self.last_pipeline_responses = []
        self._pipeline_failures_before = {}
        self._pipeline_failures_after = {}
        self._pipeline_response_errors = {}

    def _record(self, name):
        if name in self.fail_on:
            raise ConnectionError(f"simulated Redis failure on {name}")
        if self._recording:
            self.calls.append(name)
            phase = {
                "mget": "online_snapshot",
                "zrange": "desired_set_read",
                "hgetall": "desired_set_read",
                "get": "desired_set_read",
            }.get(name, name)
            self.dispatches.append(
                {
                    "phase": phase,
                    "kind": "command",
                    "operation": name,
                }
            )

    def exists(self, key):
        self._record("exists")
        return 1 if key in self.strings or key in self.zsets or key in self.hashes else 0

    def setex(self, key, ttl, value):
        self._record("setex")
        self.strings[key] = str(value)
        return True

    def mget(self, keys, *args):
        self._record("mget")
        requested = list(keys) + list(args)
        self.mget_requests.append(requested)
        if self.mget_response_override is not _NO_OVERRIDE:
            return list(self.mget_response_override)
        return [self.strings.get(key) for key in requested]

    def get(self, key):
        self._record("get")
        return self.strings.get(key)

    def incr(self, key):
        self._record("incr")
        self.strings[key] = str(int(self.strings.get(key, 0)) + 1)
        return int(self.strings[key])

    def delete(self, *keys):
        self._record("delete")
        removed = 0
        for key in keys:
            removed += key in self.strings or key in self.zsets or key in self.hashes
            self.strings.pop(key, None)
            self.zsets.pop(key, None)
            self.hashes.pop(key, None)
        return removed

    def zadd(self, key, mapping):
        self._record("zadd")
        existing = self.zsets.setdefault(key, {})
        added = sum(member not in existing for member in mapping)
        self.zsets.setdefault(key, {}).update(
            {member: float(score) for member, score in mapping.items()}
        )
        return added

    def zrange(self, key, start, end):
        self._record("zrange")
        ordered = sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))
        if end == -1:
            end = len(ordered) - 1
        return [member for member, _ in ordered[start:end + 1]]

    def hset(self, key, mapping=None):
        self._record("hset")
        existing = self.hashes.setdefault(key, {})
        added = sum(field not in existing for field in (mapping or {}))
        self.hashes.setdefault(key, {}).update(
            {field: str(value) for field, value in (mapping or {}).items()}
        )
        return added

    def hgetall(self, key):
        self._record("hgetall")
        return dict(self.hashes.get(key, {}))

    def pipeline(self, transaction=True):
        return FakePipeline(self, transaction=transaction)

    def inject_pipeline_failure(self, phase, *, when, error=None):
        if when not in {"before", "after"}:
            raise ValueError("pipeline failure timing must be 'before' or 'after'")
        target = (
            self._pipeline_failures_before
            if when == "before"
            else self._pipeline_failures_after
        )
        target[phase] = error or ConnectionError(
            "simulated pipeline failure before application"
            if when == "before"
            else "simulated pipeline acknowledgement lost after application"
        )

    def inject_pipeline_response_error(self, phase, index, error=None):
        if index < 0:
            raise ValueError("pipeline response index must be non-negative")
        self._pipeline_response_errors.setdefault(phase, {})[index] = (
            error or ResponseError("simulated pipeline command error")
        )


class FakePipeline:
    """A Redis pipeline that applies queued commands as one dispatch."""

    def __init__(self, client, transaction=True):
        self.client = client
        self.transaction = transaction
        self.phase = (
            "desired_set_publication" if transaction else "online_refresh"
        )
        self.queued = []

    def delete(self, *keys):
        self.queued.append(("delete", keys, {}))
        return self

    def zadd(self, key, mapping):
        self.queued.append(("zadd", (key, mapping), {}))
        return self

    def hset(self, key, mapping=None):
        self.queued.append(("hset", (key,), {"mapping": mapping}))
        return self

    def incr(self, key):
        self.queued.append(("incr", (key,), {}))
        return self

    def setex(self, key, ttl, value):
        self.queued.append(("setex", (key, ttl, value), {}))
        return self

    def execute(self, raise_on_error=True):
        execution = {
            "phase": self.phase,
            "transaction": self.transaction,
            "raise_on_error": raise_on_error,
            "commands": list(self.queued),
            "responses": [],
        }
        self.client.pipeline_executions.append(execution)
        self.client.calls.append("pipeline.execute")
        self.client.dispatches.append(
            {
                "phase": self.phase,
                "kind": "pipeline",
                "operation": "execute",
                "transaction": self.transaction,
            }
        )
        before_error = self.client._pipeline_failures_before.pop(
            self.phase, None
        )
        if before_error is not None:
            self.client.last_pipeline_responses = []
            execution["failure_timing"] = "before"
            self.queued = []
            raise before_error

        injected_errors = self.client._pipeline_response_errors.pop(
            self.phase, {}
        )
        responses = []
        self.client._recording = False
        try:
            for index, (name, args, kwargs) in enumerate(self.queued):
                if index in injected_errors:
                    responses.append(injected_errors[index])
                    continue
                responses.append(getattr(self.client, name)(*args, **kwargs))
        finally:
            self.client._recording = True
            self.client.last_pipeline_responses = responses
            execution["responses"] = list(responses)
            self.queued = []

        after_error = self.client._pipeline_failures_after.pop(
            self.phase, None
        )
        if after_error is not None:
            execution["failure_timing"] = "after"
            raise after_error

        if raise_on_error:
            for response in responses:
                if isinstance(response, ResponseError):
                    raise response
        return responses
