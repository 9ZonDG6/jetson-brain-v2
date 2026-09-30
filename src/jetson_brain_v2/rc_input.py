"""RC PWM input via the Linux GPIO character device (kernel line events).

Physical pin 29 -> gpiochip0 line 149 (PS5) -> CH1
Physical pin 31 -> gpiochip0 line 200 (PZ0) -> CH2

Uses GPIO_GET_LINEEVENT_IOCTL (uAPI v1, available on L4T kernel 4.9) directly,
no libgpiod needed. Pulse width = falling.timestamp - rising.timestamp, both
timestamps taken by the kernel. On 4.9 the timestamp is taken in the threaded
IRQ handler (CLOCK_REALTIME), so under heavy load expect tens of us of jitter;
a median of the last 7 pulses rejects short bursts of false stick commands.
"""

import errno
import fcntl
import logging
import os
import select
import struct
import threading
import time
from collections import deque

log = logging.getLogger("rc_input")

GPIO_CHIP = "/dev/gpiochip0"
LINES = (("CH1", 149), ("CH2", 200))

VALID_MIN_US = 900
VALID_MAX_US = 2100
LOST_TIMEOUT_MS = 100
INVALID_STREAK = 3  # consecutive out-of-range pulses before a channel is INVALID

_REQ_FMT = "<III32si"  # lineoffset, handleflags, eventflags, consumer_label[32], fd
_EVT_FMT = "<QI4x"  # timestamp ns, id (+pad) -> 16 bytes
_EVT_SIZE = struct.calcsize(_EVT_FMT)
GPIO_GET_LINEEVENT_IOCTL = 0xC030B404  # _IOWR(0xB4, 0x04, struct gpioevent_request)
GPIOHANDLE_REQUEST_INPUT = 1 << 0
GPIOEVENT_REQUEST_BOTH_EDGES = 0x3
GPIOEVENT_EVENT_RISING_EDGE = 0x01
GPIOEVENT_EVENT_FALLING_EDGE = 0x02


class _Channel:
    def __init__(self, name, line):
        self.name = name
        self.line = line
        self.fd = None
        self.last_rise_ns = None
        self.pulses = deque(maxlen=7)
        self.pulse_us = None
        self.period_us = None
        self.last_valid_mono = None
        self.last_invalid_mono = None
        self.last_edge_mono = None
        self.last_bad_us = None
        self.invalid_streak = 0


class RCInput:
    def __init__(self, chip=GPIO_CHIP, lines=LINES):
        self.chip = chip
        self.error = None
        self._channels = [_Channel(n, l) for n, l in lines]
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self):
        try:
            chip_fd = os.open(self.chip, os.O_RDONLY)
        except OSError as exc:
            self.error = "cannot open %s: %s" % (self.chip, exc.strerror)
            log.error("RC input disabled: %s", self.error)
            return
        try:
            for ch in self._channels:
                req = bytearray(struct.pack(_REQ_FMT, ch.line, GPIOHANDLE_REQUEST_INPUT,
                                            GPIOEVENT_REQUEST_BOTH_EDGES, b"robot-control", 0))
                try:
                    fcntl.ioctl(chip_fd, GPIO_GET_LINEEVENT_IOCTL, req, True)
                except OSError as exc:
                    hint = " (line busy: exported via sysfs or used by another process)" if exc.errno == errno.EBUSY else ""
                    raise RuntimeError("line %d (%s): %s%s" % (ch.line, ch.name, exc.strerror, hint))
                ch.fd = struct.unpack(_REQ_FMT, req)[4]
        except RuntimeError as exc:
            self.error = str(exc)
            log.error("RC input disabled: %s", self.error)
            self._close_fds()
            return
        finally:
            os.close(chip_fd)
        self._running = True
        self._thread = threading.Thread(target=self._run, name="rc-input", daemon=True)
        self._thread.start()
        log.info("RC input started: CH1=pin29/line149 CH2=pin31/line200")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self._close_fds()

    def snapshot(self):
        now = time.monotonic()
        out = {}
        with self._lock:
            for ch in self._channels:
                age_ms = None if ch.last_valid_mono is None else int((now - ch.last_valid_mono) * 1000)
                edge_recent = ch.last_edge_mono is not None and (now - ch.last_edge_mono) * 1000 <= LOST_TIMEOUT_MS
                latest_invalid = ch.invalid_streak >= INVALID_STREAK
                if self.error:
                    status = "LOST"
                elif age_ms is not None and age_ms <= LOST_TIMEOUT_MS and not latest_invalid:
                    status = "CONNECTED"
                elif edge_recent:
                    status = "INVALID"
                else:
                    status = "LOST"
                hz = None
                if ch.period_us and edge_recent:
                    hz = round(1e6 / ch.period_us, 1)
                out[ch.name] = {
                    "us": None if ch.pulse_us is None else int(round(ch.pulse_us)),
                    "age_ms": age_ms,
                    "hz": hz,
                    "status": status,
                    "last_bad_us": ch.last_bad_us,
                }
        return out

    # ---- internals --------------------------------------------------------

    def _close_fds(self):
        for ch in self._channels:
            if ch.fd is not None:
                try:
                    os.close(ch.fd)
                except OSError:
                    pass
                ch.fd = None

    def _run(self):
        by_fd = {ch.fd: ch for ch in self._channels}
        fds = list(by_fd)
        try:
            while self._running:
                ready, _, _ = select.select(fds, [], [], 0.1)
                for fd in ready:
                    data = os.read(fd, _EVT_SIZE * 64)
                    now = time.monotonic()
                    with self._lock:
                        ch = by_fd[fd]
                        for off in range(0, len(data) - _EVT_SIZE + 1, _EVT_SIZE):
                            ts, ev = struct.unpack_from(_EVT_FMT, data, off)
                            self._on_edge(ch, ts, ev, now)
        except Exception as exc:
            self.error = "RC reader crashed: %s" % exc
            log.error(self.error)

    @staticmethod
    def _on_edge(ch, ts, ev, now):
        ch.last_edge_mono = now
        if ev == GPIOEVENT_EVENT_RISING_EDGE:
            if ch.last_rise_ns is not None:
                period = (ts - ch.last_rise_ns) / 1000.0
                if 5000 < period < 100000:
                    ch.period_us = period if ch.period_us is None else ch.period_us * 0.8 + period * 0.2
            ch.last_rise_ns = ts
        elif ev == GPIOEVENT_EVENT_FALLING_EDGE and ch.last_rise_ns is not None:
            width = (ts - ch.last_rise_ns) / 1000.0
            if not 0 < width < 30000:
                return  # clock step or missed edges
            if VALID_MIN_US <= width <= VALID_MAX_US:
                ch.pulses.append(width)
                ch.pulse_us = sorted(ch.pulses)[len(ch.pulses) // 2]
                ch.last_valid_mono = now
                ch.invalid_streak = 0
            else:
                ch.last_bad_us = int(width)
                ch.last_invalid_mono = now
                ch.invalid_streak += 1
