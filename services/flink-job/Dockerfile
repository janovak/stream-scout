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

# Copy Flink configuration
COPY flink-conf.yaml /opt/flink/conf/flink-conf.yaml

# Copy the PyFlink job
COPY clip_detector_job.py /opt/flink/usrlib/

# Copy custom entrypoint for auto-submitting the job
COPY docker-entrypoint-job.sh /opt/flink/
RUN chmod +x /opt/flink/docker-entrypoint-job.sh

# Download Kafka connector
RUN wget -P /opt/flink/lib/ https://repo.maven.apache.org/maven2/org/apache/flink/flink-sql-connector-kafka/3.0.2-1.18/flink-sql-connector-kafka-3.0.2-1.18.jar

# Download JDBC connector
RUN wget -P /opt/flink/lib/ https://repo.maven.apache.org/maven2/org/apache/flink/flink-connector-jdbc/3.1.1-1.17/flink-connector-jdbc-3.1.1-1.17.jar

# Download PostgreSQL JDBC driver
RUN wget -P /opt/flink/lib/ https://jdbc.postgresql.org/download/postgresql-42.7.1.jar

# Download Prometheus metrics reporter
RUN wget -P /opt/flink/lib/ https://repo.maven.apache.org/maven2/org/apache/flink/flink-metrics-prometheus/1.18.0/flink-metrics-prometheus-1.18.0.jar

USER flink
