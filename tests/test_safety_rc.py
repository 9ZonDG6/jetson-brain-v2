import unittest
from unittest.mock import patch

from jetson_brain_v2.safety import FAULT, RC_ARMED, SafetyController


class Motor:
    def __init__(self):
        self.output = (1500, 1500)

    def set_output_us(self, left, right):
        self.output = (left, right)


class Receiver:
    def __init__(self):
        self.state = "CONNECTED"

    def snapshot(self):
        return {channel: {"status": self.state, "us": value}
                for channel, value in (("CH1", 1600), ("CH2", 1550))}


class RcFaultTests(unittest.TestCase):
    def setUp(self):
        self.motor = Motor()
        self.rc = Receiver()
        self.ctrl = SafetyController(self.motor, self.rc)
        self.ctrl.mode = RC_ARMED

    def tick(self, at):
        with patch("jetson_brain_v2.safety.time.monotonic", return_value=at):
            self.ctrl._tick()

    def test_short_invalid_signal_goes_neutral_then_recovers_without_rearming(self):
        self.tick(1.0)
        self.assertEqual(self.motor.output, (1600, 1550))
        self.rc.state = "INVALID"
        self.tick(1.02)
        self.tick(1.20)
        self.assertEqual(self.ctrl.mode, RC_ARMED)
        self.assertEqual(self.motor.output, (1500, 1500))
        self.rc.state = "CONNECTED"
        self.tick(1.22)
        self.assertEqual(self.ctrl.mode, RC_ARMED)
        self.assertEqual(self.motor.output, (1600, 1550))

    def test_persistent_invalid_signal_latches_fault(self):
        self.rc.state = "INVALID"
        self.tick(1.0)
        self.tick(1.26)
        self.assertEqual(self.ctrl.mode, FAULT)
        self.assertEqual(self.ctrl.fault, "RC receiver invalid")
        self.assertEqual(self.motor.output, (1500, 1500))

    def test_lost_signal_still_faults_immediately(self):
        self.rc.state = "LOST"
        self.tick(1.0)
        self.assertEqual(self.ctrl.mode, FAULT)
        self.assertEqual(self.ctrl.fault, "RC receiver lost")


if __name__ == "__main__":
    unittest.main()
