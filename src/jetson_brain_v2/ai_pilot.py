"""Drive through robot-control's existing WEB safety lease from robot-vision.

Nothing is armed until --arm is passed. A silent or disconnected vision source
ends the lease; restarting the source never silently re-arms the vehicle.
"""

import argparse
import json
import logging
import re
import signal
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import fields
from pathlib import Path

from .ai_drive import AiConfig, AiDrive, apply_rc_nudge
from .ai_source import NatsAiSource

log = logging.getLogger("ai_pilot")


def request_json(base_url, path, payload=None):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    with urllib.request.urlopen(request, timeout=0.35) as response:
        return json.load(response)


def load_config(path):
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError("AI config must be a JSON object")
    names = {field.name for field in fields(AiConfig)}
    unknown = set(raw) - names
    if unknown:
        raise ValueError("unknown AI config fields: %s" % sorted(unknown))
    return AiConfig(**raw)


ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_route(route):
    if route is None or route == "":
        return None
    if not isinstance(route, str) or not ROUTE_RE.match(route):
        raise ValueError("route must be a robot-vision route id like 1-2 or polygon")
    return route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="calibrated PWM mapping JSON")
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--nats-subject", default="robot.vision.localization")
    parser.add_argument("--nats-control-subject", default="robot.vision.control",
                        help="robot-vision control subject for set_route/resume/pause")
    parser.add_argument("--route", default=None, help="robot-vision route to select before driving")
    parser.add_argument("--control-url", default="http://127.0.0.1:8080")
    parser.add_argument("--arm", action="store_true", help="actually take the WEB control lease")
    parser.add_argument("--allow-dry-run", action="store_true", help="allow --arm against robot-control --dry-run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    try:
        config = load_config(args.config)
        route = validate_route(args.route)
    except (OSError, ValueError, TypeError) as exc:
        parser.error(str(exc))
    drive = AiDrive(config)
    source = NatsAiSource(args.nats_url, args.nats_subject, drive)
    source.start()
    client_id = "ai-" + uuid.uuid4().hex
    armed = False
    resumed = False
    stopping = False

    def stop_signal(signum, frame):
        nonlocal stopping
        stopping = True

    def control(cmd, **extra):
        payload = {"cmd": cmd}
        payload.update(extra)
        ok = source.publish(args.nats_control_subject, payload)
        log.info("robot-vision %s -> %s", payload, "sent" if ok else "FAILED")
        return ok

    signal.signal(signal.SIGINT, stop_signal)
    signal.signal(signal.SIGTERM, stop_signal)

    try:
        deadline = time.monotonic() + 10.0
        while not stopping and not source.connected:
            if source.error:
                raise RuntimeError("NATS source failed: %s" % source.error)
            if time.monotonic() > deadline:
                raise RuntimeError("could not connect to NATS at %s within 10 seconds" % args.nats_url)
            time.sleep(0.1)

        if route is not None and not stopping:
            # robot-vision ignores set_route unless its pilots are paused
            # ("сначала ПАУЗА, потом смена маршрута" in its control handler).
            if not control("pause") or not control("set_route", route=route):
                raise RuntimeError("could not send set_route to robot-vision")

        # robot-vision publishes while paused too (paused=true), so wait for
        # any message: it proves the producer is alive. Motion still needs a
        # valid command -- paused/lost/stale all map to 1500/1500 below.
        deadline = time.monotonic() + 10.0
        while not stopping:
            output, reason, valid, received_at = drive.current()
            if received_at is not None:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("no localization from robot-vision within 10 seconds")
            time.sleep(0.1)
        if stopping:
            return

        if not args.arm:
            log.info("preview only: %s -> %s; add --arm to drive", reason, output)
            return

        status = request_json(args.control_url, "/api/status")
        allowed_pwm = ("READY", "DRY-RUN") if args.allow_dry_run else ("READY",)
        if status["mode"] != "DISARMED" or status["pwm_status"] not in allowed_pwm:
            raise RuntimeError("controller must be DISARMED with expected PWM mode")
        request_json(args.control_url, "/api/arm/web", {"client_id": client_id})
        armed = True
        # robot-vision's pilot starts paused and never resumes on its own.
        resumed = control("resume")
        if not resumed:
            raise RuntimeError("could not send resume to robot-vision")
        log.info("AI armed through WEB lease; press Ctrl+C to stop")

        last_reason = None
        while not stopping:
            output, reason, valid, received_at = drive.current()
            if (reason, valid) != last_reason:
                log.info("AI: %s -> %s", reason, output if valid else (1500, 1500))
                last_reason = (reason, valid)
            if not source.connected or received_at is None or reason == "AI command timeout":
                log.error("AI source lost or timed out; stopping and disarming")
                break
            status = request_json(args.control_url, "/api/status?client_id=" + client_id)
            if status["mode"] != "WEB_ARMED" or not status["web_owner"]:
                log.info("AI control lease ended or was taken over")
                break
            if not valid:
                output = (1500, 1500)
            elif config.rc_nudge_mode != "off":
                try:
                    output = apply_rc_nudge(config, output, status)
                except ValueError as exc:
                    log.error("RC steering unavailable: %s; stopping AI", exc)
                    break
            reply = request_json(args.control_url, "/api/web-control", {
                "client_id": client_id,
                "ch1_us": output[0],
                "ch2_us": output[1],
            })
            if not reply.get("ok"):
                raise RuntimeError("controller rejected AI command")
            time.sleep(0.05)
    except (OSError, urllib.error.URLError) as exc:
        log.error("control connection failed: %s", exc)
        raise
    finally:
        if armed:
            try:
                status = request_json(args.control_url, "/api/status?client_id=" + client_id)
                if status["mode"] == "WEB_ARMED" and status["web_owner"]:
                    request_json(args.control_url, "/api/stop", {})
            except Exception:
                log.error("could not confirm STOP; controller's 500 ms lease timeout remains active")
        if resumed:
            control("pause")
        source.close()


if __name__ == "__main__":
    main()
