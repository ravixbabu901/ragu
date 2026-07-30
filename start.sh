#!/bin/sh

curl -sLO ${XRAY_CONFIG}
xray -c xray.json >/dev/null 2>&1 &
cd /home
python3 -m userge
