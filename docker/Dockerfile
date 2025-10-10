# ----------------------------------------------------------------------
# STAGE 1: BUILDER (For installing and compiling Python packages)
# ----------------------------------------------------------------------
# Use the base image that includes all necessary build tools
FROM --platform=linux/arm64 python:3.11-slim-bookworm AS builder

# Install build-time and runtime system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libffi-dev \
    pkg-config \
    # Runtime libs needed to satisfy python packages
    libgpiod2 \
    python3-libgpiod \
    i2c-tools \
    sudo \
    tzdata \
    util-linux \
    # Cleanup unnecessary files in this stage
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && \
    pip uninstall -y gpiod || true && \
    pip install --no-cache-dir --force-reinstall -r requirements.txt

# ----------------------------------------------------------------------
# STAGE 2: RUNTIME (The final, minimal image)
# ----------------------------------------------------------------------
# Start from the same clean base image
FROM --platform=linux/arm64 python:3.11-slim-bookworm AS runtime

# Install ONLY the necessary system libraries required at runtime
# Note: --no-install-recommends keeps the image minimal
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgpiod2 \
    python3-libgpiod \
    i2c-tools \
    sudo \
    tzdata \
    util-linux \
    # 💡 Final cleanup for minimal size
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 💡 COPY STEP 1: Copy ONLY the installed Python packages from the builder stage
# This is where the bulk of the size reduction comes from.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages

# 💡 COPY STEP 2: Copy Python executables (like gunicorn) installed by pip
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy your application script [cite: 4]
COPY presto_x728_sysmon.py .
RUN mkdir -p /config

# Create user and groups using the same GIDs [cite: 5, 6]
RUN addgroup --gid 993 gpio && \
    getent group i2c || addgroup --gid 994 i2c && \
    getent group disk || addgroup --gid 6 disk && \
    adduser --disabled-password --uid 1000 --ingroup gpio appuser && \
    usermod -a -G i2c appuser && \
    chown -R appuser:gpio /app /config

# Sudoers setup remains the same
RUN echo "appuser ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/reboot" > /etc/sudoers.d/appuser && \
    chmod 0440 /etc/sudoers.d/appuser

# ENV, EXPOSE, HEALTHCHECK, USER, and CMD remain the same
ENV PATH="/usr/local/bin:$PATH"
ENV PYTHONPATH="/usr/local/lib/python3.11/site-packages:/usr/lib/python3/dist-packages"


EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000', timeout=5)" || exit 1 [cite: 7]
    
USER 1000:993
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gevent", "--timeout", "120", "presto_x728_sysmon:app"]