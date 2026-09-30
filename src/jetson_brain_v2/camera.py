"""Single camera capture shared by MJPEG preview and local AVI recording."""

import logging
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("camera")
MAX_FRAME_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class CameraConfig:
    kind: str = "usb"
    device: str = "/dev/video0"
    url: str = ""
    width: int = 640
    height: int = 480
    fps: int = 15
    input_format: str = "mjpeg"
    command: tuple = ()

    def __post_init__(self):
        if self.kind not in ("usb", "rtsp", "test", "command"):
            raise ValueError("camera kind must be usb, rtsp, test or command")
        if type(self.width) is not int or type(self.height) is not int or type(self.fps) is not int:
            raise ValueError("camera dimensions and fps must be integers")
        if not 1 <= self.width <= 3840 or not 1 <= self.height <= 2160 or not 1 <= self.fps <= 60:
            raise ValueError("camera dimensions or fps out of range")
        if self.kind == "usb" and not self.device.startswith("/dev/video"):
            raise ValueError("USB camera device must be /dev/videoN")
        if self.kind == "rtsp" and not self.url.startswith(("rtsp://", "rtsps://")):
            raise ValueError("RTSP camera URL is invalid")
        if self.input_format not in ("mjpeg", "yuyv422"):
            raise ValueError("USB input_format must be mjpeg or yuyv422")
        if self.kind == "command" and (not isinstance(self.command, (list, tuple)) or
                                       not self.command or any(not isinstance(part, str) for part in self.command)):
            raise ValueError("camera command must be an argument list that writes MJPEG to stdout")

    def capture_command(self):
        if self.kind == "command":
            return list(self.command)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
        if self.kind == "usb":
            cmd += ["-f", "v4l2", "-input_format", self.input_format,
                    "-video_size", "%dx%d" % (self.width, self.height),
                    "-framerate", str(self.fps), "-i", self.device]
        elif self.kind == "rtsp":
            cmd += ["-rtsp_transport", "tcp", "-i", self.url]
        else:
            cmd += ["-re", "-f", "lavfi", "-i", "testsrc=size=%dx%d:rate=%d" % (
                self.width, self.height, self.fps)]
        if self.kind == "usb" and self.input_format == "mjpeg":
            cmd += ["-an", "-c:v", "copy"]
        else:
            cmd += ["-an", "-vf", "scale=%d:%d,fps=%d" % (self.width, self.height, self.fps),
                    "-c:v", "mjpeg", "-q:v", "5"]
        return cmd + ["-f", "mjpeg", "pipe:1"]


def jpeg_frames(stream):
    """Yield complete JPEG images from ffmpeg's concatenated MJPEG stream."""
    buffer = bytearray()
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        buffer.extend(chunk)
        while True:
            start = buffer.find(b"\xff\xd8")
            if start < 0:
                buffer.clear()
                break
            if start:
                del buffer[:start]
            end = buffer.find(b"\xff\xd9", 2)
            if end < 0:
                if len(buffer) > MAX_FRAME_BYTES:
                    buffer.clear()
                break
            frame = bytes(buffer[:end + 2])
            del buffer[:end + 2]
            yield frame


class CameraManager:
    def __init__(self, config, recordings_dir):
        self.config = config
        self.recordings_dir = Path(recordings_dir)
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._stopping = threading.Event()
        self._capture_thread = None
        self._capture_process = None
        self._latest = None
        self._frame_id = 0
        self._last_frame_at = None
        self._record_queue = None
        self._record_thread = None
        self._record_process = None
        self._record_path = None
        self._preview_lock = threading.Lock()
        self.error = None

    def recordings(self):
        if not self.recordings_dir.is_dir():
            return []
        with self._lock:
            active = self._record_path.name if self._record_queue is not None else None
        files = []
        for path in self.recordings_dir.iterdir():
            if path.suffix.lower() != ".avi" or not path.is_file() or path.is_symlink():
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            files.append({"name": path.name, "size": stat.st_size,
                          "modified": stat.st_mtime, "recording": path.name == active})
        return sorted(files, key=lambda item: item["modified"], reverse=True)

    def recording_file(self, name):
        if not name or name != Path(name).name or not name.lower().endswith(".avi"):
            raise FileNotFoundError(name)
        path = self.recordings_dir / name
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(name)
        return path

    def preview_file(self, name):
        source = self.recording_file(name)
        with self._lock:
            if self._record_queue is not None and self._record_path == source:
                raise RuntimeError("stop recording before viewing it")
        preview = self.recordings_dir / ".preview" / (name + ".mp4")
        with self._preview_lock:
            if preview.is_file() and preview.stat().st_mtime_ns >= source.stat().st_mtime_ns:
                return preview
            preview.parent.mkdir(parents=True, exist_ok=True)
            temporary = preview.with_suffix(".tmp")
            try:
                subprocess.run([
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                    "-i", str(source), "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", str(temporary),
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                os.replace(temporary, preview)
            finally:
                temporary.unlink(missing_ok=True)
        return preview

    def prepared_preview_file(self, name):
        source = self.recording_file(name)
        preview = self.recordings_dir / ".preview" / (name + ".mp4")
        if preview.is_file() and not preview.is_symlink() and preview.stat().st_mtime_ns >= source.stat().st_mtime_ns:
            return preview
        return None

    def delete_recording(self, name):
        with self._lock:
            source = self.recording_file(name)
            if self._record_queue is not None and self._record_path == source:
                raise RuntimeError("stop recording before deleting it")
        with self._preview_lock:
            source.unlink()
            (self.recordings_dir / ".preview" / (name + ".mp4")).unlink(missing_ok=True)

    def start(self):
        if self.config is None or self._capture_thread is not None:
            return
        self._capture_thread = threading.Thread(target=self._capture_loop, name="camera", daemon=True)
        self._capture_thread.start()

    def stop(self):
        self._stopping.set()
        process = self._capture_process
        if process is not None and process.poll() is None:
            process.terminate()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=3)
        self.stop_recording()

    def status(self):
        with self._lock:
            age = None if self._last_frame_at is None else time.monotonic() - self._last_frame_at
            return {
                "configured": self.config is not None,
                "online": age is not None and age < 3.0,
                "recording": self._record_queue is not None,
                "file": None if self._record_path is None else str(self._record_path),
                "error": self.error,
            }

    def wait_frame(self, after_id, timeout=2.0):
        with self._condition:
            self._condition.wait_for(lambda: self._frame_id != after_id or self._stopping.is_set(), timeout)
            if self._stopping.is_set():
                return None, None
            return self._frame_id, self._latest

    def start_recording(self):
        if self.config is None:
            raise RuntimeError("camera is not configured")
        with self._lock:
            if self._record_queue is not None:
                raise RuntimeError("camera is already recording")
            if self._last_frame_at is None or time.monotonic() - self._last_frame_at > 3:
                raise RuntimeError("camera has no live frames")
            self.recordings_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.recordings_dir / ("camera-%s.avi" % timestamp)
            if path.exists():
                raise RuntimeError("recording filename already exists")
            process = subprocess.Popen([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-f", "mjpeg", "-r", str(self.config.fps), "-i", "pipe:0",
                "-an", "-c:v", "copy", "-f", "avi", str(path),
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            frames = queue.Queue(maxsize=self.config.fps * 4)
            self._record_process = process
            self._record_queue = frames
            self._record_path = path
            self._record_thread = threading.Thread(
                target=self._record_loop, args=(process, frames), name="camera-record", daemon=True)
            self._record_thread.start()
            log.info("camera recording started: %s", path)
            return str(path)

    def stop_recording(self):
        with self._lock:
            frames = self._record_queue
            writer = self._record_thread
            self._record_queue = None
        if frames is None:
            return None
        try:
            frames.put(None, timeout=1)
        except queue.Full:
            frames.get_nowait()
            frames.put_nowait(None)
        writer.join(timeout=5)
        process = self._record_process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        log.info("camera recording stopped: %s", self._record_path)
        return None if self._record_path is None else str(self._record_path)

    def _record_loop(self, process, frames):
        try:
            while True:
                frame = frames.get()
                if frame is None:
                    break
                process.stdin.write(frame)
            process.stdin.close()
            if process.wait(timeout=5) != 0:
                self.error = "camera recording muxer failed"
        except Exception as exc:
            self.error = "camera recording failed: %s" % exc
            log.error(self.error)

    def _capture_loop(self):
        while not self._stopping.is_set():
            try:
                with open("/tmp/jetson-brain-v2-camera.log", "ab", buffering=0) as log_file:
                    process = subprocess.Popen(self.config.capture_command(), stdout=subprocess.PIPE,
                                               stderr=log_file, bufsize=0)
                    self._capture_process = process
                    for frame in jpeg_frames(process.stdout):
                        if self._stopping.is_set():
                            break
                        with self._condition:
                            self._latest = frame
                            self._frame_id += 1
                            self._last_frame_at = time.monotonic()
                            self.error = None
                            frames = self._record_queue
                            self._condition.notify_all()
                        if frames is not None:
                            try:
                                frames.put_nowait(frame)
                            except queue.Full:
                                self.error = "recording cannot keep up; frames dropped"
                    process.stdout.close()
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=2)
                    if not self._stopping.is_set():
                        self.error = "camera capture stopped; retrying"
            except Exception as exc:
                self.error = "camera capture failed: %s" % exc
                log.error(self.error)
            if not self._stopping.wait(1.0):
                continue
