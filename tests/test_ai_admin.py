import tempfile
import unittest
from pathlib import Path

from jetson_brain_v2.ai_admin import AiAdmin, config_from_wasd, log_tail, pilot_command
from jetson_brain_v2.ai_pilot import validate_route


class AiAdminTests(unittest.TestCase):
    def test_browser_swap_and_inversion_become_physical_side_mapping(self):
        config = config_from_wasd({
            "swap": True, "inv1": True, "inv2": False,
            "drive_delta_us": 80, "turn_delta_us": 60,
            "rc_nudge_mode": "tank",
        })
        self.assertEqual((config.left_output, config.left_sign), (2, 1))
        self.assertEqual((config.right_output, config.right_sign), (1, -1))

    def test_saved_mapping_roundtrips_and_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as directory:
            admin = AiAdmin(Path(directory) / "ai.json")
            body = {"swap": False, "inv1": False, "inv2": True}
            admin.save_wasd_config(body)
            self.assertEqual(admin.config().right_sign, -1)
            with self.assertRaisesRegex(ValueError, "swap must be boolean"):
                admin.save_wasd_config({"swap": 1, "inv1": False, "inv2": False})

    def test_rc_steering_side_follows_the_wasd_swap(self):
        plain = config_from_wasd({"swap": False, "inv1": False, "inv2": False, "rc_steering_side": "right"})
        swapped = config_from_wasd({"swap": True, "inv1": False, "inv2": False, "rc_steering_side": "right"})
        self.assertEqual(plain.rc_steering_channel, 2)
        self.assertEqual(swapped.rc_steering_channel, 1)
        with self.assertRaisesRegex(ValueError, "rc_steering_side"):
            config_from_wasd({"swap": False, "inv1": False, "inv2": False, "rc_steering_side": "up"})


class PilotCommandTests(unittest.TestCase):
    def test_route_is_forwarded_to_the_pilot_only_when_set(self):
        with_route = pilot_command("c.json", "nats://h:4222", "http://127.0.0.1:8080", route="polygon")
        self.assertEqual(with_route[-2:], ["--route", "polygon"])
        self.assertNotIn("--route", pilot_command("c.json", "nats://h:4222", "http://127.0.0.1:8080"))

    def test_route_names_are_validated(self):
        self.assertIsNone(validate_route(""))
        self.assertEqual(validate_route("1-2-short"), "1-2-short")
        for bad in ("../x", "a b", "-lead", "x" * 65):
            with self.assertRaises(ValueError):
                validate_route(bad)

    def test_start_rejects_bad_route_before_spawning(self):
        with tempfile.TemporaryDirectory() as directory:
            admin = AiAdmin(Path(directory) / "ai.json")
            admin.save_wasd_config({"swap": False, "inv1": False, "inv2": False})
            with self.assertRaises(ValueError):
                admin.start("nats://h:4222", "http://127.0.0.1:8080", route="a b")
            self.assertFalse(admin.status()["running"])

    def test_log_tail_returns_last_lines_and_tolerates_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ai.log"
            self.assertEqual(log_tail(path), [])
            path.write_text("\n".join("line %d" % i for i in range(20)) + "\n")
            self.assertEqual(log_tail(path, lines=3), ["line 17", "line 18", "line 19"])


if __name__ == "__main__":
    unittest.main()
