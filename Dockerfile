FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    gnupg \
    gcc \
    aria2 \
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

RUN mkdir -p /etc/apt/keyrings \
    && wget -qO- https://mkvtoolnix.download/gpg-pub-moritzbunkus.gpg \
       | gpg --dearmor -o /etc/apt/keyrings/mkvtoolnix.gpg \
    && chmod a+r /etc/apt/keyrings/mkvtoolnix.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/mkvtoolnix.gpg] https://mkvtoolnix.download/debian/ bookworm main" \
       > /etc/apt/sources.list.d/mkvtoolnix.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends mkvtoolnix \
    && rm -rf /var/lib/apt/lists/*

RUN wget -qO- https://github.com/ginuerzh/gost/releases/download/v2.12.0/gost_2.12.0_linux_amd64.tar.gz | tar -xzf - -C /usr/local/bin/ gost

COPY --from=cwhuntx/static-ffmpeg /ffmpeg /usr/bin/
COPY --from=cwhuntx/static-ffmpeg /ffprobe /usr/bin/

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/logs /home

WORKDIR /home

COPY userge ./userge

CMD ["python", "-m", "userge"]
