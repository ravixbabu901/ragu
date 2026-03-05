FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
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

# Install mkvtoolnix from official repo (Debian bookworm)
RUN mkdir -p /etc/apt/keyrings \
    && wget -qO /etc/apt/keyrings/mkvtoolnix.gpg https://mkvtoolnix.download/gpg-pub-mks.asc \
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/mkvtoolnix.gpg] https://mkvtoolnix.download/debian/ bookworm main" \
       > /etc/apt/sources.list.d/mkvtoolnix.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/downloads

CMD ["python", "-m", "userge"]
