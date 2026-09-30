"""Manual focus controls for USB UVC cameras via the V4L2 character device."""

import fcntl
import os
import struct

VIDIOC_QUERYCTRL = 0xC0445624
VIDIOC_G_CTRL = 0xC008561B
VIDIOC_S_CTRL = 0xC008561C
FOCUS_ABSOLUTE = 0x009A090A
FOCUS_AUTO = 0x009A090C
_QUERY_FORMAT = "=II32siiiiIII"
_CONTROL_FORMAT = "=Ii"


def _control(fd, command, control_id, value=0):
    data = bytearray(struct.pack(_CONTROL_FORMAT, control_id, value))
    fcntl.ioctl(fd, command, data, True)
    return struct.unpack(_CONTROL_FORMAT, data)[1]


def _range(fd):
    data = bytearray(struct.pack(_QUERY_FORMAT, FOCUS_ABSOLUTE, 0, b"", 0, 0, 0, 0, 0, 0, 0))
    fcntl.ioctl(fd, VIDIOC_QUERYCTRL, data, True)
    _, _, _, minimum, maximum, step, _, _, _, _ = struct.unpack(_QUERY_FORMAT, data)
    return minimum, maximum, step


def focus_status(device):
    fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
    try:
        minimum, maximum, step = _range(fd)
        return {
            "auto": bool(_control(fd, VIDIOC_G_CTRL, FOCUS_AUTO)),
            "value": _control(fd, VIDIOC_G_CTRL, FOCUS_ABSOLUTE),
            "min": minimum, "max": maximum, "step": step,
        }
    finally:
        os.close(fd)


def set_focus(device, *, auto=None, value=None):
    fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
    try:
        minimum, maximum, step = _range(fd)
        if auto is not None:
            if type(auto) is not bool:
                raise ValueError("auto must be true or false")
            _control(fd, VIDIOC_S_CTRL, FOCUS_AUTO, int(auto))
        if value is not None:
            if type(value) is not int or value < minimum or value > maximum or (value - minimum) % step:
                raise ValueError("focus value is out of range")
            if _control(fd, VIDIOC_G_CTRL, FOCUS_AUTO):
                raise ValueError("switch to manual focus first")
            _control(fd, VIDIOC_S_CTRL, FOCUS_ABSOLUTE, value)
        return {
            "auto": bool(_control(fd, VIDIOC_G_CTRL, FOCUS_AUTO)),
            "value": _control(fd, VIDIOC_G_CTRL, FOCUS_ABSOLUTE),
            "min": minimum, "max": maximum, "step": step,
        }
    finally:
        os.close(fd)
