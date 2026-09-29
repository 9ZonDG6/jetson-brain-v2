"""Hardware PWM outputs for Jetson Nano (Tegra210, L4T R32.7.x).

Physical pin 32 -> pwmchip0/pwm0 (PV0, LCD_BL_PWM), pinmux 0x700031fc = 0x45 (PWM0)
Physical pin 33 -> pwmchip0/pwm2 (PE6),            pinmux 0x70003248 = 0x46 (PWM2)

Only hardware PWM via sysfs is used. Tegra210 PWM has an 8-bit duty cycle
(256 steps per period), so at 20 ms the real output step is 78.125 us:
1000 -> 1016, 1500 -> 1484, 2000 -> 2031 us. get_actual_us() reports that.
"""

import logging
import mmap
import os
import struct
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("motor_io")

PERIOD_NS = 20_000_000
NEUTRAL_US = 1500
MIN_US = 1000
MAX_US = 2000
DUTY_STEPS = 256
STEP_NS = PERIOD_NS / DUTY_STEPS  # 78125 ns
HYSTERESIS_NS = 15_000  # input noise must cross a step boundary by this much to switch steps
NEUTRAL_DEADBAND_US = 25  # anything this close to 1500 is output as the neutral step
PWMCHIP = "/sys/class/pwm/pwmchip0"


@dataclass(frozen=True)
class _Channel:
    name: str
    pin: int
    pwm: int
    pinmux_reg: int
    pinmux_val: int
    gpio_cnf_reg: int
    gpio_bit: int


CHANNELS = (
    _Channel("OUT1", 32, 0, 0x700031FC, 0x45, 0x6000D504, 0),
    _Channel("OUT2", 33, 2, 0x70003248, 0x46, 0x6000D100, 6),
)


def clamp_us(value):
    """Any garbage (None, str, NaN, inf, -500, 9000) becomes a value in MIN..MAX."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return NEUTRAL_US
    if v != v:  # NaN
        return NEUTRAL_US
    return int(round(min(MAX_US, max(MIN_US, v))))


def quantize_us(us):
    return int(round(round(us * 1000 / STEP_NS) * STEP_NS / 1000))


def _next_step(us, current):
    """Duty step index for `us`, sticking to `current` unless clearly past the boundary."""
    if abs(us - NEUTRAL_US) <= NEUTRAL_DEADBAND_US:
        us = NEUTRAL_US
    cand = int(round(us * 1000 / STEP_NS))
    if current is None or us == NEUTRAL_US or cand == current:
        return cand
    if abs(us * 1000 - current * STEP_NS) <= STEP_NS / 2 + HYSTERESIS_NS:
        return current
    return cand


def _mem_read32(addr):
    fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
    try:
        base = addr & ~0xFFF
        m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ, offset=base)
        try:
            return struct.unpack_from("<I", m, addr - base)[0]
        finally:
            m.close()
    finally:
        os.close(fd)


def _mem_write32(addr, value):
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        base = addr & ~0xFFF
        m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=base)
        try:
            struct.pack_into("<I", m, addr - base, value)
        finally:
            m.close()
    finally:
        os.close(fd)


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def _read(path):
    with open(path) as f:
        return f.read().strip()


class MotorIO:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.ready = False
        self.error = None
        self._lock = threading.Lock()
        self._out = (NEUTRAL_US, NEUTRAL_US)
        self._step = [None, None]

    # ---- public API -------------------------------------------------------

    def initialize(self):
        with self._lock:
            self._out = (NEUTRAL_US, NEUTRAL_US)
            if self.dry_run:
                self.ready = True
                print("WOULD SET PWM: %d %d" % self._out, flush=True)
                log.info("PWM initialized (dry-run, hardware untouched)")
                return
            try:
                # 1) PWM generators first: period + neutral duty, enabled.
                for ch in CHANNELS:
                    self._setup_pwm(ch)
                # 2) Only then route the pads to PWM, so the pin never sees garbage.
                for ch in CHANNELS:
                    self._setup_pinmux(ch)
                self._step = [_next_step(NEUTRAL_US, None)] * 2
                self.ready = True
                self.error = None
                log.info("PWM initialized: pin32=pwm0 pin33=pwm2, 50 Hz, 1500/1500 us")
            except Exception as exc:
                self.ready = False
                self.error = "PWM init failed: %s" % exc
                raise

    def set_output_us(self, ch1, ch2):
        a, b = clamp_us(ch1), clamp_us(ch2)
        with self._lock:
            if not self.ready:
                raise RuntimeError(self.error or "PWM not initialized")
            if self.dry_run:
                if (a, b) != self._out:
                    print("WOULD SET PWM: %d %d" % (a, b), flush=True)
                self._out = (a, b)
                return self._out
            try:
                for i, (ch, us) in enumerate(zip(CHANNELS, (a, b))):
                    step = _next_step(us, self._step[i])
                    if step != self._step[i]:
                        _write("%s/pwm%d/duty_cycle" % (PWMCHIP, ch.pwm), int(step * STEP_NS))
                        self._step[i] = step
                self._out = (a, b)
            except Exception as exc:
                self.error = "PWM write failed: %s" % exc
                raise
            return self._out

    def stop(self):
        return self.set_output_us(NEUTRAL_US, NEUTRAL_US)

    def get_output_us(self):
        return self._out

    def get_actual_us(self):
        if self.dry_run or None in self._step:
            return tuple(quantize_us(v) for v in self._out)
        return tuple(int(round(s * STEP_NS / 1000)) for s in self._step)

    def shutdown(self):
        """Leave both outputs generating neutral 1500 us (deterministic stop signal)."""
        if not self.ready:
            return
        try:
            self.stop()
            log.info("PWM left at neutral 1500/1500")
        except Exception as exc:
            log.error("PWM shutdown: could not set neutral: %s", exc)

    # ---- internals --------------------------------------------------------

    def _setup_pwm(self, ch):
        p = "%s/pwm%d" % (PWMCHIP, ch.pwm)
        if not os.path.isdir(p):
            _write(PWMCHIP + "/export", ch.pwm)
            deadline = time.monotonic() + 2.0
            while not os.path.exists(p + "/enable"):
                if time.monotonic() > deadline:
                    raise RuntimeError("export of pwm%d timed out" % ch.pwm)
                time.sleep(0.02)
        # Kernel rejects duty writes while period == 0 (fresh boot) and period
        # writes shorter than the current duty, so: duty 0 (if a period exists),
        # then period, then neutral duty.
        if int(_read(p + "/period")) != 0:
            _write(p + "/duty_cycle", 0)
        _write(p + "/period", PERIOD_NS)
        _write(p + "/duty_cycle", NEUTRAL_US * 1000)
        _write(p + "/enable", 1)
        if int(_read(p + "/period")) != PERIOD_NS or int(_read(p + "/duty_cycle")) != NEUTRAL_US * 1000:
            raise RuntimeError("pwm%d readback mismatch" % ch.pwm)

    def _setup_pinmux(self, ch):
        cnf = _mem_read32(ch.gpio_cnf_reg)
        if cnf & (1 << ch.gpio_bit):
            # Masked CNF register (+0x80): bit (8+n) = write-enable, bit n = 0 -> SFIO.
            _mem_write32(ch.gpio_cnf_reg + 0x80, 1 << (8 + ch.gpio_bit))
        _mem_write32(ch.pinmux_reg, ch.pinmux_val)
        got = _mem_read32(ch.pinmux_reg)
        if got & 0x7F != ch.pinmux_val & 0x7F:
            raise RuntimeError("pinmux 0x%08x readback 0x%x" % (ch.pinmux_reg, got))
        if _mem_read32(ch.gpio_cnf_reg) & (1 << ch.gpio_bit):
            raise RuntimeError("pin %d still in GPIO mode" % ch.pin)
