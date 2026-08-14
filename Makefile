.PHONY: test test-api-frontend test-stream-monitoring test-flink-job clean-venvs

SERVICES := api-frontend stream-monitoring flink-job

# apache-flink and confluent-kafka only ship prebuilt wheels for Python 3.10
# (this project's `apache-flink==1.18.0` pin predates 3.11 support, and
# confluent-kafka has no 3.14 wheel yet); building either from source drags
# in a full C toolchain plus librdkafka headers, or just fails outright.
# Pinned in .mise.toml; invoked here via `mise exec` so it works whether or
# not mise's shell activation is set up.
PY := mise exec python@3.10 -- python3.10

test:
	@status=0; \
	for svc in $(SERVICES); do \
		$(MAKE) test-$$svc || status=1; \
	done; \
	exit $$status

test-api-frontend: services/api-frontend/.venv/.installed
	cd services/api-frontend && .venv/bin/python -m pytest

test-stream-monitoring: services/stream-monitoring/.venv/.installed
	cd services/stream-monitoring && .venv/bin/python -m pytest

test-flink-job: services/flink-job/.venv/.installed
	cd services/flink-job && .venv/bin/python -m pytest

services/%/.venv/.installed: services/%/requirements-dev.txt
	rm -rf services/$*/.venv
	cd services/$* && $(PY) -m venv .venv
	cd services/$* && .venv/bin/pip install --upgrade pip -q
	cd services/$* && .venv/bin/pip install -r requirements-dev.txt -q
	touch $@

clean-venvs:
	rm -rf services/api-frontend/.venv services/stream-monitoring/.venv services/flink-job/.venv
