"""Validate robot-vision commands and mix them for two 4WD side outputs.

The localization producer decides where to go. This module only accepts fresh,
well-formed commands and maps them to the motor controller's calibrated channels.
"""

import json
import math
import threading
import time
from dataclasses import dataclass

from .motor_io import NEUTRAL_US, clamp_us


@dataclass(frozen=True)
class AiConfig:
    left_output: int
    right_output: int
    left_sign: int
    right_sign: int
    drive_delta_us: int
    turn_delta_us: int
    full_steering_angle_deg: float = 30.0
    min_inliers: int = 20
    timeout_s: float = 0.5
    max_source_age_s: float = 0.5
    rc_nudge_mode: str = "off"
    rc_steering_channel: int = 1
    rc_steering_sign: int = 1
    rc_nudge_limit_us: int = 75
    rc_nudge_deadband_us: int = 40

    def __post_init__(self):
        if type(self.left_output) is not int or type(self.right_output) is not int:
            raise ValueError("set AI left_output and right_output to 1 or 2")
        if sorted((self.left_output, self.right_output)) != [1, 2]:
            raise ValueError("AI left and right outputs must be distinct: 1 and 2")
        if type(self.left_sign) is not int or type(self.right_sign) is not int or self.left_sign not in (-1, 1) or self.right_sign not in (-1, 1):
            raise ValueError("AI output signs must be -1 or 1")
        if type(self.drive_delta_us) is not int or not 1 <= self.drive_delta_us <= 500:
            raise ValueError("AI drive_delta_us must be in 1..500")
        if type(self.turn_delta_us) is not int or not 1 <= self.turn_delta_us <= 500:
            raise ValueError("AI turn_delta_us must be in 1..500")
        if type(self.min_inliers) is not int or not 0 <= self.min_inliers <= 100000:
            raise ValueError("AI min_inliers must be nonnegative")
        for name in ("full_steering_angle_deg", "timeout_s", "max_source_age_s"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("AI %s must be finite and positive" % name)
        if self.rc_nudge_mode not in ("off", "tank", "channel"):
            raise ValueError("AI rc_nudge_mode must be off, tank or channel")
        if self.rc_steering_channel not in (1, 2) or self.rc_steering_sign not in (-1, 1):
            raise ValueError("AI RC steering channel/sign is invalid")
        if type(self.rc_nudge_limit_us) is not int or not 0 <= self.rc_nudge_limit_us <= 250:
            raise ValueError("AI rc_nudge_limit_us must be in 0..250")
        if type(self.rc_nudge_deadband_us) is not int or not 0 <= self.rc_nudge_deadband_us <= 100:
            raise ValueError("AI rc_nudge_deadband_us must be in 0..100")


class AiDrive:
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._received_at = None
        self._output = (NEUTRAL_US, NEUTRAL_US)
        self._reason = "no AI command received"
        self._valid = False

    def ingest(self, payload, now=None, wall_now=None):
        """Replace the previous decision, including when this payload is invalid."""
        now = time.monotonic() if now is None else now
        wall_now = time.time() if wall_now is None else wall_now
        try:
            output, reason, valid = self._parse(payload, wall_now)
        except (TypeError, ValueError, KeyError) as exc:
            output, reason, valid = (NEUTRAL_US, NEUTRAL_US), "invalid AI command: %s" % exc, False
        with self._lock:
            self._received_at = now
            self._output = output
            self._reason = reason
            self._valid = valid

    def current(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            received_at = self._received_at
            output = self._output
            reason = self._reason
            valid = self._valid
        if received_at is None:
            return (NEUTRAL_US, NEUTRAL_US), reason, False, None
        age = now - received_at
        if age < 0 or age > self.config.timeout_s:
            return (NEUTRAL_US, NEUTRAL_US), "AI command timeout", False, received_at
        return output, reason, valid, received_at

    def _parse(self, payload, wall_now):
        if isinstance(payload, bytes):
            if len(payload) > 16384:
                raise ValueError("payload too large")
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            if len(payload) > 16384:
                raise ValueError("payload too large")
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")

        move = payload.get("move_type")
        if move not in ("straight", "left", "right", "stop", "lost"):
            raise ValueError("unknown move_type")
        if "paused" in payload and type(payload["paused"]) is not bool:
            raise ValueError("paused must be boolean")
        if "valid" in payload and type(payload["valid"]) is not bool:
            raise ValueError("valid must be boolean")
        if move in ("stop", "lost") or payload.get("paused") is True or payload.get("valid") is False:
            return (NEUTRAL_US, NEUTRAL_US), "AI %s" % move, False

        ts = payload.get("ts")
        if type(ts) not in (int, float) or not math.isfinite(ts):
            raise ValueError("producer timestamp missing or invalid")
        if wall_now - ts > self.config.max_source_age_s or ts - wall_now > 5.0:
            raise ValueError("producer timestamp stale or in future")
        inliers = payload.get("inliers")
        if type(inliers) is not int or inliers < self.config.min_inliers:
            raise ValueError("localization inliers below minimum")
        deg = payload.get("deg")
        if type(deg) not in (int, float) or not math.isfinite(deg):
            raise ValueError("angle missing or invalid")
        if abs(deg) > 90:
            raise ValueError("angle exceeds 90 degrees")
        if move == "left" and deg >= 0 or move == "right" and deg <= 0:
            raise ValueError("turn sign disagrees with move_type")
        if move == "straight" and abs(deg) > 8:
            raise ValueError("straight angle exceeds 8 degrees")
        direction = payload.get("direction", "forward")
        if direction != "forward":
            raise ValueError("only forward AI driving is configured")

        turn_ratio = 0.0 if move == "straight" else max(-1.0, min(1.0, deg / self.config.full_steering_angle_deg))
        turn = round(turn_ratio * self.config.turn_delta_us)
        left = max(0, min(500, self.config.drive_delta_us + turn))
        right = max(0, min(500, self.config.drive_delta_us - turn))
        out = [NEUTRAL_US, NEUTRAL_US]
        out[self.config.left_output - 1] = clamp_us(NEUTRAL_US + self.config.left_sign * left)
        out[self.config.right_output - 1] = clamp_us(NEUTRAL_US + self.config.right_sign * right)
        return tuple(out), "AI %s" % move, True


def apply_rc_nudge(config, ai_output, status):
    """Add RC steering to a valid AI command without changing its throttle sign.

    The RC receiver already forwards CH1/CH2 to OUT1/OUT2 in manual mode.
    In tank mode their differential component is steering; common throttle is
    ignored. In channel mode one calibrated receiver channel is steering.
    """
    if config.rc_nudge_mode == "off":
        return ai_output
    if not status.get("rc_valid"):
        raise ValueError("RC signal lost during AI steering")
    ch1 = status.get("ch1_us")
    ch2 = status.get("ch2_us")
    if type(ch1) is not int or type(ch2) is not int:
        raise ValueError("RC channel pulse missing")
    if config.rc_nudge_mode == "tank":
        left_rc = (status["ch%d_us" % config.left_output] - NEUTRAL_US) * config.left_sign
        right_rc = (status["ch%d_us" % config.right_output] - NEUTRAL_US) * config.right_sign
        turn = (left_rc - right_rc) / 2.0
    else:
        turn = (status["ch%d_us" % config.rc_steering_channel] - NEUTRAL_US) * config.rc_steering_sign
    if abs(turn) <= config.rc_nudge_deadband_us:
        turn = 0
    turn = max(-config.rc_nudge_limit_us, min(config.rc_nudge_limit_us, round(turn)))
    left = (ai_output[config.left_output - 1] - NEUTRAL_US) * config.left_sign
    right = (ai_output[config.right_output - 1] - NEUTRAL_US) * config.right_sign
    out = list(ai_output)
    out[config.left_output - 1] = clamp_us(NEUTRAL_US + config.left_sign * max(0, min(500, left + turn)))
    out[config.right_output - 1] = clamp_us(NEUTRAL_US + config.right_sign * max(0, min(500, right - turn)))
    return tuple(out)
