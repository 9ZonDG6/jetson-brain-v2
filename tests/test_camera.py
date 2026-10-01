import shutil
import tempfile
import time
import unittest
import zipfile
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
                first = camera.take_photo()
                second = camera.take_photo()
                self.assertNotEqual(first["name"], second["name"])
                self.assertEqual(second["count"], 2)
                with zipfile.ZipFile(camera.photo_archive_path) as album:
                    self.assertEqual(len(album.namelist()), 2)
                    self.assertTrue(all(album.read(name).startswith(b"\xff\xd8") for name in album.namelist()))
                self.assertGreater(camera.photo_archive()["size"], 1000)
                time.sleep(0.8)
                self.assertTrue(camera.status()["recording"])
                camera.stop_recording()
                self.assertGreater(Path(path).stat().st_size, 1000)
                self.assertFalse(camera.status()["recording"])
                self.assertEqual(camera.recordings()[0]["name"], Path(path).name)
                preview = camera.preview_file(Path(path).name)
                self.assertGreater(preview.stat().st_size, 1000)
                self.assertEqual(camera.preview_file(Path(path).name), preview)
                self.assertEqual(camera.prepared_preview_file(Path(path).name), preview)
                with self.assertRaises(FileNotFoundError):
                    camera.recording_file("../outside.avi")
                camera.delete_recording(Path(path).name)
                self.assertFalse(Path(path).exists())
                self.assertFalse(preview.exists())
                camera.delete_photos()
                self.assertEqual(camera.photo_archive(), {"count": 0, "size": 0})
            finally:
                camera.stop()


if __name__ == "__main__":
    unittest.main()
