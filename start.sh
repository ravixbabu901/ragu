#!/bin/sh

curl -sLo /app/xray.json ${XRAY_CONFIG}
xray -c /app/xray.json >/dev/null 2>&1 &
cd /home
python3 -m userge
