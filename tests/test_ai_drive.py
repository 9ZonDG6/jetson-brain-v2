import json
import unittest

from jetson_brain_v2.ai_drive import AiConfig, AiDrive, apply_rc_nudge


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

    def test_stale_timestamp_reason_reports_clock_offset(self):
        self.drive.ingest({"move_type": "straight", "deg": 0, "inliers": 30, "ts": 100}, now=10, wall_now=103.5)
        _output, reason, valid, _ = self.drive.current(now=10.1)
        self.assertFalse(valid)
        self.assertIn("3.50 s old", reason)
        self.assertIn("clock sync", reason)

    def test_paused_producer_is_received_but_not_valid(self):
        self.drive.ingest({"move_type": "straight", "deg": 0, "inliers": 30, "ts": 100, "paused": True},
                          now=10, wall_now=100)
        output, reason, valid, received_at = self.drive.current(now=10.1)
        self.assertEqual(reason, "AI paused by robot-vision")
        self.assertEqual(output, (1500, 1500))
        self.assertFalse(valid)
        self.assertEqual(received_at, 10)

    def test_tank_rc_steering_changes_turn_without_adding_throttle(self):
        config = AiConfig(
            left_output=2, right_output=1, left_sign=1, right_sign=1,
            drive_delta_us=50, turn_delta_us=50, rc_nudge_mode="tank",
        )
        status = {"rc_valid": True, "ch1_us": 1450, "ch2_us": 1550}
        self.assertEqual(apply_rc_nudge(config, (1550, 1550), status), (1500, 1600))

    def test_lost_rc_signal_blocks_nudge(self):
        config = AiConfig(
            left_output=1, right_output=2, left_sign=1, right_sign=1,
            drive_delta_us=50, turn_delta_us=50, rc_nudge_mode="tank",
        )
        with self.assertRaisesRegex(ValueError, "RC signal lost"):
            apply_rc_nudge(config, (1550, 1550), {"rc_valid": False})


if __name__ == "__main__":
    unittest.main()
