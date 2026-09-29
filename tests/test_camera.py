import shutil
import tempfile
import time
import unittest
from pathlib import Path

from jetson_brain_v2.camera import CameraConfig, CameraManager


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for camera test")
class CameraTests(unittest.TestCase):
    def test_one_capture_feeds_preview_and_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            camera = CameraManager(CameraConfig(kind="test", width=160, height=120, fps=5), directory)
            camera.start()
            try:
                frame_id, frame = camera.wait_frame(0, timeout=5)
                self.assertGreater(frame_id, 0)
                self.assertTrue(frame.startswith(b"\xff\xd8"))
                path = camera.start_recording()
                time.sleep(0.8)
                self.assertTrue(camera.status()["recording"])
                camera.stop_recording()
                self.assertGreater(Path(path).stat().st_size, 1000)
                self.assertFalse(camera.status()["recording"])
            finally:
                camera.stop()


if __name__ == "__main__":
    unittest.main()
