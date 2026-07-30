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

COPY --from=cwhuntx/static-ffmpeg /ffmpeg /usr/bin/
COPY --from=cwhuntx/static-ffmpeg /ffprobe /usr/bin/

WORKDIR /app

RUN curl -sLO https://github.com/XTLS/Xray-core/releases/download/v24.11.11/Xray-linux-64.zip \
    && unzip -j Xray-linux-64.zip xray -d /usr/local/bin && rm Xray-linux-64.zip

COPY requirements.txt start.sh .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/logs /home && chmod +x start.sh

WORKDIR /home

COPY userge ./userge

CMD ["/app/start.sh"]
