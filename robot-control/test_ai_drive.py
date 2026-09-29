import json
import unittest

from ai_drive import AiConfig, AiDrive


class AiDriveTests(unittest.TestCase):
    def setUp(self):
        self.drive = AiDrive(AiConfig(
            left_output=2,
            right_output=1,
            left_sign=1,
            right_sign=1,
            drive_delta_us=50,
            turn_delta_us=50,
        ))

    def test_fresh_left_turn_slows_left_side_using_calibrated_channels(self):
        self.drive.ingest(json.dumps({
            "move_type": "left", "deg": -30, "inliers": 30,
            "ts": 100,
        }), now=10, wall_now=100.1)
        self.assertEqual(self.drive.current(now=10.1)[:3], ((1600, 1500), "AI left", True))

    def test_reverse_is_neutral_until_calibrated_for_differential_drive(self):
        self.drive.ingest({
            "move_type": "left", "deg": -20, "inliers": 30,
            "direction": "backward", "ts": 100,
        }, now=10, wall_now=100)
        self.assertEqual(self.drive.current(now=10.1)[0], (1500, 1500))

    def test_invalid_message_replaces_previous_movement_with_neutral(self):
        self.drive.ingest({"move_type": "straight", "deg": 0, "inliers": 30, "ts": 100}, now=10, wall_now=100)
        self.drive.ingest({"move_type": "left", "deg": 20, "inliers": 30, "ts": 100}, now=10.1, wall_now=100.1)
        output, _, valid, _ = self.drive.current(now=10.2)
        self.assertEqual(output, (1500, 1500))
        self.assertFalse(valid)

    def test_stale_producer_and_receiver_timeout_are_neutral(self):
        self.drive.ingest({"move_type": "straight", "deg": 0, "inliers": 30, "ts": 100}, now=10, wall_now=101)
        self.assertFalse(self.drive.current(now=10.1)[2])
        self.drive.ingest({"move_type": "straight", "deg": 0, "inliers": 30, "ts": 100}, now=11, wall_now=100)
        output, reason, valid, _ = self.drive.current(now=11.6)
        self.assertEqual(output, (1500, 1500))
        self.assertEqual(reason, "AI command timeout")
        self.assertFalse(valid)


if __name__ == "__main__":
    unittest.main()
