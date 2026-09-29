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

log = logging.getLogger("ai_admin")


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
    return AiConfig(
        left_output=left_output,
        right_output=right_output,
        left_sign=signs[left_output],
        right_sign=signs[right_output],
        drive_delta_us=body.get("drive_delta_us", 80),
        turn_delta_us=body.get("turn_delta_us", 80),
        rc_nudge_mode=body.get("rc_nudge_mode", "off"),
        rc_steering_channel=body.get("rc_steering_channel", 1),
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

    def start(self, nats_url, control_url, allow_dry_run=False):
        parsed = urlparse(nats_url)
        if parsed.scheme not in ("nats", "tls") or not parsed.hostname:
            raise ValueError("enter a NATS URL such as nats://host:4222")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise ValueError("AI is already starting or running")
            self.config()  # validate stored mapping before starting a child
            if not self.config_path.exists():
                raise ValueError("save the WASD output mapping before starting AI")
            self._log_file = open("/tmp/jetson-brain-v2-ai.log", "ab", buffering=0)
            command = [
                sys.executable, "-m", "jetson_brain_v2.ai_pilot",
                "--config", str(self.config_path),
                "--nats-url", nats_url,
                "--control-url", control_url,
                "--arm",
            ]
            if allow_dry_run:
                command.append("--allow-dry-run")
            self._process = subprocess.Popen(command, stdout=self._log_file, stderr=subprocess.STDOUT, start_new_session=True)
            log.info("AI pilot started, pid=%s", self._process.pid)
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
        return {
            "configured": self.config_path.exists(),
            "running": process is not None and process.poll() is None,
            "exit_code": None if process is None else process.poll(),
            "log_path": "/tmp/jetson-brain-v2-ai.log",
        }
