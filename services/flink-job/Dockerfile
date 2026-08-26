FROM flink:1.18-scala_2.12-java11

# Install Python and pip
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-dev \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/python3 /usr/bin/python

# Install Python dependencies
COPY requirements.txt /opt/flink/
RUN pip3 install --no-cache-dir -r /opt/flink/requirements.txt

# Download the Flink connector JARs before copying any job source. These
# layers depend only on the base image and the apt/pip layers above, none
# of which change often. Placing them here keeps their cache valid when a
# routine job-code change (below) invalidates the layers under it, so a
# rebuild of this image does not re-download every JAR from the network
# just because clip_detector_job.py changed.

# Download Kafka connector
RUN wget -P /opt/flink/lib/ https://repo.maven.apache.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.0.2-1.18/flink-sql-connector-kafka-3.0.2-1.18.jar

# Download JDBC connector
RUN wget -P /opt/flink/lib/ https://repo.maven.apache.org/maven2/org/apache/flink/flink-connector-jdbc/3.1.1-1.17/flink-connector-jdbc-3.1.1-1.17.jar

# Download PostgreSQL JDBC driver
RUN wget -P /opt/flink/lib/ https://jdbc.postgresql.org/download/postgresql-42.7.1.jar

# Download Prometheus metrics reporter
RUN wget -P /opt/flink/lib/ https://repo.maven.apache.org/maven2/org/apache/flink/flink-metrics-prometheus/1.18.0/flink-metrics-prometheus-1.18.0.jar

# Copy Flink configuration
COPY flink-conf.yaml /opt/flink/conf/flink-conf.yaml

# Copy the PyFlink job
COPY clip_detector_job.py /opt/flink/usrlib/
COPY clip_attempt.py /opt/flink/usrlib/
COPY spike_detector.py /opt/flink/usrlib/
COPY token_manager.py /opt/flink/usrlib/

# Copy the test suite so it can run in-container as a fallback to the host venv
COPY test_clip_detector.py /opt/flink/usrlib/
COPY test_clip_attempt.py /opt/flink/usrlib/
COPY test_spike_detector.py /opt/flink/usrlib/

# Copy the custom entrypoint. It runs the job under Flink Application
# Mode -- there is no separate submission step.
COPY docker-entrypoint-job.sh /opt/flink/
RUN chmod +x /opt/flink/docker-entrypoint-job.sh

USER flink
