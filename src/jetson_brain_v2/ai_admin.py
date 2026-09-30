"""Explicit AI lease management for the operator dashboard."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

from .ai_drive import AiConfig
from .ai_pilot import validate_route

log = logging.getLogger("ai_admin")
AI_LOG_PATH = "/tmp/jetson-brain-v2-ai.log"


def pilot_command(config_path, nats_url, control_url, allow_dry_run=False, route=None):
    command = [
        sys.executable, "-m", "jetson_brain_v2.ai_pilot",
        "--config", str(config_path),
        "--nats-url", nats_url,
        "--control-url", control_url,
        "--arm",
    ]
    if route is not None:
        command += ["--route", route]
    if allow_dry_run:
        command.append("--allow-dry-run")
    return command


def log_tail(path, lines=8, max_bytes=4096):
    """Last few pilot log lines, so the dashboard shows *why* AI is idle."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


def config_from_wasd(body):
    """Use the operator's proven browser WASD swap/inversion wiring."""
    for key in ("swap", "inv1", "inv2"):
        if type(body.get(key)) is not bool:
            raise ValueError("%s must be boolean" % key)
    swap = body["swap"]
    inv1 = body["inv1"]
    inv2 = body["inv2"]
    left_output = 2 if swap else 1
    right_output = 1 if swap else 2
    signs = {1: -1 if inv1 else 1, 2: -1 if inv2 else 1}
    # The RC receiver drives OUT1/OUT2 directly, so each RC channel is one
    # robot side; the operator picks a side and it maps through the same
    # swap as WASD. rc_steering_channel is still accepted for old clients.
    side = body.get("rc_steering_side")
    if side is not None:
        if side not in ("left", "right"):
            raise ValueError("rc_steering_side must be left or right")
        rc_channel = left_output if side == "left" else right_output
    else:
        rc_channel = body.get("rc_steering_channel", 1)
    return AiConfig(
        left_output=left_output,
        right_output=right_output,
        left_sign=signs[left_output],
        right_sign=signs[right_output],
        drive_delta_us=body.get("drive_delta_us", 80),
        turn_delta_us=body.get("turn_delta_us", 80),
        rc_nudge_mode=body.get("rc_nudge_mode", "off"),
        rc_steering_channel=rc_channel,
        rc_steering_sign=body.get("rc_steering_sign", 1),
        rc_nudge_limit_us=body.get("rc_nudge_limit_us", 75),
    )


class AiAdmin:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self._lock = threading.Lock()
        self._process = None
        self._log_file = None

    def config(self):
        if not self.config_path.exists():
            return None
        data = json.loads(self.config_path.read_text())
        return AiConfig(**data)

    def save_wasd_config(self, body):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ValueError("stop AI before changing its configuration")
            config = config_from_wasd(body)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.config_path.with_suffix(".json.tmp")
            from dataclasses import asdict
            temp.write_text(json.dumps(asdict(config), indent=2) + "\n")
            os.replace(temp, self.config_path)
            return config

    def start(self, nats_url, control_url, allow_dry_run=False, route=None):
        parsed = urlparse(nats_url)
        if parsed.scheme not in ("nats", "tls") or not parsed.hostname:
            raise ValueError("enter a NATS URL such as nats://host:4222")
        route = validate_route(route)
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ValueError("AI is already starting or running")
            if not self.config_path.exists():
                raise ValueError("save the WASD output mapping before starting AI")
            self.config()  # validate stored mapping before starting a child
            self._log_file = open(AI_LOG_PATH, "ab", buffering=0)
            command = pilot_command(self.config_path, nats_url, control_url, allow_dry_run, route)
            self._process = subprocess.Popen(command, stdout=self._log_file, stderr=subprocess.STDOUT, start_new_session=True)
            log.info("AI pilot started, pid=%s, route=%s", self._process.pid, route)
            return self.status()

    def stop(self):
        with self._lock:
            process = self._process
            if process is not None and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1.0)
            if self._log_file is not None:
                self._log_file.close()
                self._log_file = None
            return self.status()

    def status(self):
        process = self._process
        config = None
        config_error = None
        if self.config_path.exists():
            try:
                from dataclasses import asdict
                config = asdict(self.config())
            except (OSError, ValueError, TypeError) as exc:
                config_error = str(exc)
        return {
            "configured": self.config_path.exists(),
            "config": config,
            "config_error": config_error,
            "running": process is not None and process.poll() is None,
            "exit_code": None if process is None else process.poll(),
            "log_path": AI_LOG_PATH,
            "log_tail": log_tail(AI_LOG_PATH),
        }
