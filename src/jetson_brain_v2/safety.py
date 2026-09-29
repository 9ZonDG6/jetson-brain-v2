"""Safety state machine: DISARMED / RC_ARMED / WEB_ARMED / FAULT.

Runs its own 50 Hz thread, independent of the web event loop, so watchdogs
keep working even if the HTTP side stalls. Every path that is not an armed,
healthy state produces 1500/1500. Leaving FAULT requires an explicit ARM.
"""

import logging
import threading
import time

from .motor_io import NEUTRAL_US, clamp_us

log = logging.getLogger("safety")

TICK_S = 0.02
HEARTBEAT_TIMEOUT_S = 0.5
RC_ARM_CENTER_TOL_US = 100

DISARMED = "DISARMED"
RC_ARMED = "RC_ARMED"
WEB_ARMED = "WEB_ARMED"
FAULT = "FAULT"

NEUTRAL = (NEUTRAL_US, NEUTRAL_US)


class ArmError(Exception):
    pass


class SafetyController:
    def __init__(self, motor, rc):
        self.motor = motor
        self.rc = rc
        self.mode = DISARMED
        self.fault = None
        self._web = NEUTRAL
        self._owner = None
        self._last_hb = 0.0
        self._rc_overall = None
        self._shutting_down = False
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread = None

    # ---- lifecycle --------------------------------------------------------

    def start(self):
        self._apply(NEUTRAL)
        self._thread = threading.Thread(target=self._loop, name="safety", daemon=True)
        self._thread.start()

    def begin_shutdown(self):
        """Refuse any further ARM/control and go neutral right away."""
        with self._lock:
            self._shutting_down = True
            if self.mode != DISARMED:
                self.stop("STOP (shutdown)")

    def shutdown(self):
        self.begin_shutdown()
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        with self._lock:
            self.mode = DISARMED
            self._owner = None
            self._web = NEUTRAL
        self.motor.shutdown()

    # ---- commands ---------------------------------------------------------

    def arm_rc(self):
        with self._lock:
            self._require_pwm()
            rc = self.rc.snapshot()
            if self.rc.error:
                raise ArmError("RC input unavailable: %s" % self.rc.error)
            for name in ("CH1", "CH2"):
                if rc[name]["status"] != "CONNECTED":
                    raise ArmError("%s is %s" % (name, rc[name]["status"]))
                if abs(rc[name]["us"] - NEUTRAL_US) > RC_ARM_CENTER_TOL_US:
                    raise ArmError("%s not centered (%d us), center the sticks" % (name, rc[name]["us"]))
            self._owner = None
            self._web = NEUTRAL
            self.fault = None
            self.mode = RC_ARMED
            self._apply(NEUTRAL)
            log.info("ARM RC")

    def arm_web(self, client_id):
        if not client_id:
            raise ArmError("client_id required")
        with self._lock:
            self._require_pwm()
            self._web = NEUTRAL
            self._owner = client_id
            self._last_hb = time.monotonic()
            self.fault = None
            self.mode = WEB_ARMED
            self._apply(NEUTRAL)
            log.info("ARM WEB")

    def stop(self, reason="STOP"):
        with self._lock:
            self.mode = DISARMED
            self.fault = None
            self._owner = None
            self._web = NEUTRAL
            self._apply(NEUTRAL)
            log.info(reason)

    def set_web(self, client_id, ch1, ch2):
        with self._lock:
            if self.mode != WEB_ARMED or client_id != self._owner or self._shutting_down:
                return False
            self._web = (clamp_us(ch1), clamp_us(ch2))
            self._last_hb = time.monotonic()
            self._apply(self._web)
            return True

    def heartbeat(self, client_id):
        with self._lock:
            if self.mode == WEB_ARMED and client_id == self._owner:
                self._last_hb = time.monotonic()
                return True
            return False

    def client_disconnected(self, client_id):
        with self._lock:
            if self.mode == WEB_ARMED and client_id == self._owner:
                self._fault("browser disconnected")

    def status(self, client_id=None):
        rc = self.rc.snapshot()
        with self._lock:
            out = self.motor.get_output_us()
            actual = self.motor.get_actual_us()
            return {
                "mode": self.mode,
                "fault": self.fault,
                "rc_status": self._rc_overall_status(rc),
                "rc_valid": all(rc[c]["status"] == "CONNECTED" for c in ("CH1", "CH2")),
                "rc_error": self.rc.error,
                "ch1_us": rc["CH1"]["us"],
                "ch2_us": rc["CH2"]["us"],
                "ch1_age_ms": rc["CH1"]["age_ms"],
                "ch2_age_ms": rc["CH2"]["age_ms"],
                "ch1_hz": rc["CH1"]["hz"],
                "ch2_hz": rc["CH2"]["hz"],
                "ch1_status": rc["CH1"]["status"],
                "ch2_status": rc["CH2"]["status"],
                "out1_us": out[0],
                "out2_us": out[1],
                "out1_actual_us": actual[0],
                "out2_actual_us": actual[1],
                "web1_us": self._web[0],
                "web2_us": self._web[1],
                "web_owner": client_id is not None and client_id == self._owner,
                "pwm_status": ("DRY-RUN" if self.motor.dry_run else "READY") if self.motor.ready else "ERROR",
                "pwm_error": self.motor.error,
                "dry_run": self.motor.dry_run,
            }

    # ---- internals --------------------------------------------------------

    def _require_pwm(self):
        if self._shutting_down:
            raise ArmError("backend is shutting down")
        if not self.motor.ready:
            raise ArmError("PWM output not ready: %s" % self.motor.error)

    @staticmethod
    def _rc_overall_status(rc):
        st = [rc[c]["status"] for c in ("CH1", "CH2")]
        if all(s == "CONNECTED" for s in st):
            return "CONNECTED"
        if "INVALID" in st:
            return "INVALID"
        return "LOST"

    def _fault(self, reason):
        self.mode = FAULT
        self.fault = reason
        self._owner = None
        self._web = NEUTRAL
        self._apply(NEUTRAL)
        log.warning("FAULT: %s", reason)

    def _apply(self, target):
        try:
            self.motor.set_output_us(*target)
        except Exception as exc:
            if self.mode != FAULT:
                self.mode = FAULT
                self.fault = "PWM error: %s" % exc
                log.error("FAULT: %s", self.fault)
            try:
                self.motor.stop()
            except Exception:
                pass

    def _loop(self):
        while not self._stop_evt.wait(TICK_S):
            try:
                self._tick()
            except Exception as exc:
                with self._lock:
                    self._fault("internal error: %s" % exc)

    def _tick(self):
        rc = self.rc.snapshot()
        with self._lock:
            overall = self._rc_overall_status(rc)
            if overall != self._rc_overall:
                if self._rc_overall is not None or overall == "CONNECTED":
                    log.info("RC %s", overall.lower() if overall != "INVALID" else "invalid signal")
                self._rc_overall = overall

            if self.mode == RC_ARMED:
                if overall != "CONNECTED":
                    self._fault("RC receiver %s" % overall.lower())
                else:
                    self._apply((rc["CH1"]["us"], rc["CH2"]["us"]))
            elif self.mode == WEB_ARMED:
                if time.monotonic() - self._last_hb > HEARTBEAT_TIMEOUT_S:
                    self._fault("browser heartbeat lost > %d ms" % (HEARTBEAT_TIMEOUT_S * 1000))
                else:
                    self._apply(self._web)
            else:
                self._apply(NEUTRAL)
