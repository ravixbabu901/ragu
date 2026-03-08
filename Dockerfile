FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    gnupg \
    gcc \
    aria2 \
    ffmpeg \
    git \
    curl \
    wget \
    zip \
    unzip \
    procps \
    p7zip-full \
    pv \
    jq \
    xz-utils \
    gzip \
    mediainfo \
    && rm -rf /var/lib/apt/lists/*

# mkvtoolnix repo (bookworm) — proper keyring (no apt-key)
RUN mkdir -p /etc/apt/keyrings \
    && wget -qO- https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg \
       | gpg --dearmor -o /etc/apt/keyrings/mkvtoolnix.gpg \
    && chmod a+r /etc/apt/keyrings/mkvtoolnix.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/mkvtoolnix.gpg] https://mkvtoolnix.download/debian/ bookworm main" \
       > /etc/apt/sources.list.d/mkvtoolnix.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

# Put everything in /usr/src/app (system location, not user-facing)
WORKDIR /usr/src/app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY userge ./userge
RUN mkdir -p /home /downloads /app/logs

# Set working directory to /downloads - this is empty and user-facing
WORKDIR /downloads

CMD ["python", "-m", "userge"]
