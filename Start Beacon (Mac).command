#!/bin/bash
# Starts Beacon from the source (the installed Beacon.app doesn't need this).
cd "$(dirname "$0")"
nohup python3 laser_status_app.py "$@" >/dev/null 2>&1 &
