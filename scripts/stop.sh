#!/usr/bin/env bash
# Stop robot-control: SIGTERM (outputs -> 1500/1500), SIGKILL only if it hangs.
PIDFILE=/run/robot-control.pid
[ -f "$PIDFILE" ] || { echo "not running (no $PIDFILE)"; exit 0; }
PID=$(cat "$PIDFILE")
kill -TERM "$PID" 2>/dev/null || { echo "not running"; rm -f "$PIDFILE"; exit 0; }
for _ in $(seq 50); do kill -0 "$PID" 2>/dev/null || { echo "stopped"; tail -3 /tmp/robot-control.log; exit 0; }; sleep 0.1; done
echo "did not stop in 5 s, SIGKILL (PWM stays at its last value!)"; kill -KILL "$PID"; rm -f "$PIDFILE"
