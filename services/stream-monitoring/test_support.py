"""Shared in-memory adapters for stream-monitoring unit tests."""


class FakeRedis:
    """Enough Redis for the poller/reconciler seam, held in memory."""

    def __init__(self):
        self.strings = {}
        self.zsets = {}
        self.hashes = {}
        self.calls = []
        self.fail_on = set()
        self._recording = True

    def _record(self, name):
        if name in self.fail_on:
            raise ConnectionError(f"simulated Redis failure on {name}")
        if self._recording:
            self.calls.append(name)

    def exists(self, key):
        self._record("exists")
        return 1 if key in self.strings or key in self.zsets or key in self.hashes else 0

    def setex(self, key, ttl, value):
        self._record("setex")
        self.strings[key] = str(value)

    def get(self, key):
        self._record("get")
        return self.strings.get(key)

    def incr(self, key):
        self._record("incr")
        self.strings[key] = str(int(self.strings.get(key, 0)) + 1)
        return int(self.strings[key])

    def delete(self, *keys):
        self._record("delete")
        for key in keys:
            self.strings.pop(key, None)
            self.zsets.pop(key, None)
            self.hashes.pop(key, None)

    def zadd(self, key, mapping):
        self._record("zadd")
        self.zsets.setdefault(key, {}).update(
            {member: float(score) for member, score in mapping.items()}
        )

    def zrange(self, key, start, end):
        self._record("zrange")
        ordered = sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))
        if end == -1:
            end = len(ordered) - 1
        return [member for member, _ in ordered[start:end + 1]]

    def hset(self, key, mapping=None):
        self._record("hset")
        self.hashes.setdefault(key, {}).update(
            {field: str(value) for field, value in (mapping or {}).items()}
        )

    def hgetall(self, key):
        self._record("hgetall")
        return dict(self.hashes.get(key, {}))

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    """A MULTI/EXEC that applies its queued commands as one round trip."""

    def __init__(self, client):
        self.client = client
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

    def execute(self):
        self.client._recording = False
        try:
            for name, args, kwargs in self.queued:
                if name == "delete":
                    self.client.delete(*args)
                else:
                    getattr(self.client, name)(*args, **kwargs)
        finally:
            self.client._recording = True
        self.client.calls.append("pipeline.execute")
        self.queued = []
