"""Runtime CPU latency tuning for accurate RC edge timestamps (reverted on exit).

Measured on Jetson Nano / L4T 4.9 with RC at 1500 us, 4 s of pulses:
  schedutil + C7 enabled        : p5..p95 = 1440..1634 us, ~45% of pulses off by >20 us
  performance + C7 enabled      : ~21% off by >20 us
  performance + C7 disabled     : 1483..1507 us, 0% off by >20 us
Both changes are sysfs-only and not persistent (reset on reboot).
"""

import glob
import logging

log = logging.getLogger("cpu_tuning")

GOVERNOR_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor"
DEEP_IDLE_GLOB = "/sys/devices/system/cpu/cpu[0-9]*/cpuidle/state[1-9]*/disable"


class CpuTuning:
    def __init__(self):
        self._saved = {}

    def apply(self):
        for path in glob.glob(GOVERNOR_GLOB):
            self._set(path, "performance")
        for path in glob.glob(DEEP_IDLE_GLOB):
            self._set(path, "1")
        if self._saved:
            log.info("CPU tuning: governor=performance, deep idle (C7) disabled")

    def restore(self):
        for path, value in self._saved.items():
            try:
                with open(path, "w") as f:
                    f.write(value)
            except OSError as exc:
                log.warning("CPU tuning restore %s: %s", path, exc)
        if self._saved:
            log.info("CPU tuning restored")
        self._saved.clear()

    def _set(self, path, value):
        try:
            with open(path) as f:
                old = f.read().strip()
            if old != value:
                with open(path, "w") as f:
                    f.write(value)
                self._saved[path] = old
        except OSError as exc:
            log.warning("CPU tuning %s: %s", path, exc)
