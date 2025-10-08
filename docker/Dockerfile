FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y \
    build-essential \
    python3-dev \
    libffi-dev \
    libgpiod2 \
    python3-libgpiod \
    i2c-tools \
    sudo \
    tzdata \
    util-linux \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip && \
    pip uninstall -y gpiod || true && \
    pip install --no-cache-dir --force-reinstall -r requirements.txt

WORKDIR /app
COPY presto_x728_sysmon.py .
RUN mkdir -p /config

# Create user and add to gpio and i2c groups (use host GID for gpio, e.g., 997)
RUN addgroup --gid 993 gpio && \
    getent group i2c || addgroup --gid 994 i2c && \
    getent group disk || addgroup --gid 6 disk && \
    adduser --disabled-password --uid 1000 --ingroup gpio appuser && \
    usermod -a -G i2c appuser && \
    chown -R appuser:gpio /app /config

RUN echo "appuser ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/reboot" > /etc/sudoers.d/appuser && \
    chmod 0440 /etc/sudoers.d/appuser

ENV PATH="/usr/local/bin:$PATH"
ENV PYTHONPATH="/usr/local/lib/python3.11/site-packages:/usr/lib/python3/dist-packages"

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000', timeout=5)" || exit 1
USER 1000:993
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--worker-class", "gevent", "--timeout", "120", "presto_x728:app"]
