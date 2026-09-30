import unittest

from jetson_brain_v2.rc_input import (
    GPIOEVENT_EVENT_FALLING_EDGE,
    GPIOEVENT_EVENT_RISING_EDGE,
    RCInput,
    _Channel,
)


class RcInputFilterTests(unittest.TestCase):
    def test_short_valid_looking_burst_does_not_move_stick(self):
        channel = _Channel("CH2", 200)

        def pulse(index, width):
            rise_ns = index * 20_000_000
            now = index * 0.02
            RCInput._on_edge(channel, rise_ns, GPIOEVENT_EVENT_RISING_EDGE, now)
            RCInput._on_edge(channel, rise_ns + width * 1000,
                             GPIOEVENT_EVENT_FALLING_EDGE, now)

        for i in range(7):
            pulse(i, 1500)
        for i in range(7, 10):
            pulse(i, 1682)
            self.assertEqual(channel.pulse_us, 1500)
        pulse(10, 1500)
        self.assertEqual(channel.pulse_us, 1500)

        # A real stick change persists for four samples and is still accepted.
        for i in range(11, 18):
            pulse(i, 1800)
        self.assertEqual(channel.pulse_us, 1800)


if __name__ == "__main__":
    unittest.main()
