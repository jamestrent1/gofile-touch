FROM python:3.11-slim

# ---------------------------------------------------------------------------
# System dependencies: Firefox ESR + GeckoDriver for Selenium
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        firefox-esr \
        wget \
        ca-certificates \
        bzip2 \
    && rm -rf /var/lib/apt/lists/*

# Install GeckoDriver (Firefox WebDriver)
ARG GECKODRIVER_VERSION=0.34.0
RUN set -eux; \
    ARCH="$(uname -m)"; \
    case "$ARCH" in \
        x86_64)  PLATFORM="linux64" ;; \
        aarch64) PLATFORM="linux-aarch64" ;; \
        *)       echo "Unsupported architecture: $ARCH" && exit 1 ;; \
    esac; \
    wget -q "https://github.com/mozilla/geckodriver/releases/download/v${GECKODRIVER_VERSION}/geckodriver-v${GECKODRIVER_VERSION}-${PLATFORM}.tar.gz" \
         -O /tmp/geckodriver.tar.gz; \
    tar -xzf /tmp/geckodriver.tar.gz -C /usr/local/bin; \
    rm /tmp/geckodriver.tar.gz; \
    chmod +x /usr/local/bin/geckodriver; \
    geckodriver --version

# ---------------------------------------------------------------------------
# Python application
# ---------------------------------------------------------------------------
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .

# Print Python output immediately (no buffering)
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
