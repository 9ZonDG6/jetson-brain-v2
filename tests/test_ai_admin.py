import tempfile
import unittest
from pathlib import Path

from jetson_brain_v2.ai_admin import AiAdmin, config_from_wasd


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


if __name__ == "__main__":
    unittest.main()
