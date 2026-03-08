#!/bin/sh
# entrypoint.sh — run the app from /app while keeping container WORKDIR=/empty
# This ensures interactive shells land in /empty but the process runs with /app as CWD.

# create writable downloads dir if not present (uses config.sample DOWN_PATH by default)
mkdir -p /app/downloads
# switch to app dir for the actual process
cd /app

# Exec the bot. The repo uses 'userge' package — run that module.
exec python -m userge
