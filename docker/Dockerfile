# Build stage
FROM python:3.11-slim-bookworm AS builder

# Install build dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    libffi-dev \
    pkg-config \
    libsystemd-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && \
    pip uninstall -y gpiod || true && \
    pip install --no-cache-dir --force-reinstall --user -r requirements.txt

# Runtime stage
FROM python:3.11-slim-bookworm

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libgpiod2 \
    python3-libgpiod \
    i2c-tools \
    sudo \
    tzdata \
    util-linux \
    libsystemd0 \
    && rm -rf /var/lib/apt/lists/*

# Copy Python dependencies from build stage
COPY --from=builder /root/.local /root/.local

# Set up application
WORKDIR /app
COPY presto_x728_sysmon.py .
RUN mkdir -p /config

# Create user and add to gpio, i2c, disk groups (unchanged)
RUN addgroup --gid 993 gpio && \
    getent group i2c || addgroup --gid 994 i2c && \
    getent group disk || addgroup --gid 6 disk && \
    adduser --disabled-password --uid 1000 --ingroup gpio appuser && \
    usermod -a -G i2c appuser && \
    chown -R appuser:gpio /app /config

# Set up sudoers for appuser (unchanged)
RUN echo "appuser ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/reboot" > /etc/sudoers.d/appuser && \
    chmod 0440 /etc/sudoers.d/appuser

# Environment variables (unchanged)
ENV PATH="/usr/local/bin:/root/.local/bin:$PATH"
ENV PYTHONPATH="/usr/local/lib/python3.11/site-packages:/usr/lib/python3/dist-packages"

EXPOSE 7728

# Healthcheck (unchanged)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:7728', timeout=5)" || exit 1

# Default to appuser (overridden by docker-compose.yml user: "0:0")
USER 1000:993

CMD ["gunicorn", "--bind", "0.0.0.0:7728", "--workers", "1", "--worker-class", "gevent", "--timeout", "120", "presto_x728_sysmon:app"]