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

RUN wget -qO- https://github.com/ginuerzh/gost/releases/download/v2.12.0/gost_2.12.0_linux_amd64.tar.gz \
    | tar -xzf - -C /usr/local/bin/ gost

COPY --from=cwhuntx/static-ffmpeg /ffmpeg /usr/bin/
COPY --from=cwhuntx/static-ffmpeg /ffprobe /usr/bin/

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -U pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt
RUN mkdir -p /app/logs /home

#RUN curl -sLO https://github.com/XTLS/Xray-core/releases/download/v24.11.11/Xray-linux-64.zip \
#    && unzip -j Xray-linux-64.zip xray -d /usr/local/bin && rm Xray-linux-64.zip
#RUN curl -sLO https://gist.githubusercontent.com/vijay-kumar2/5d7068a4f7101ce9110bf29ac445a17d/raw/xray.json
#RUN pip install --no-cache-dir -U pysocks

WORKDIR /home

COPY userge ./userge

#CMD ["python", "-m", "userge"]
CMD sh -c "gost -L http://127.0.0.1:8084 -F 'wss://chand:warish@gosteu-ead73ca51cc7.herokuapp.com:443' >/dev/null 2>&1 & exec python -m userge"
#CMD sh -c "xray -c /app/xray.json >/dev/null 2>&1 & exec python -m userge"
