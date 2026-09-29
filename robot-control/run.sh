#!/usr/bin/env bash
# Start robot-control in the background (root needed for PWM/pinmux/GPIO). Extra args go to app.py.
set -e
cd "$(dirname "$0")"
if [ -f /run/robot-control.pid ] && kill -0 "$(cat /run/robot-control.pid)" 2>/dev/null; then
    echo "already running, pid $(cat /run/robot-control.pid)"; exit 1
fi
setsid nohup .venv/bin/python app.py "$@" > /tmp/robot-control.log 2>&1 < /dev/null &
sleep 3
cat /tmp/robot-control.log
