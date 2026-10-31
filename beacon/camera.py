"""Webcam snapshots for "job finished" messages. Uses OpenCV when it's
installed (pip install opencv-python-headless); otherwise available() is False
and the feature explains how to turn it on."""

import sys


def _backend(cv2):
    """OpenCV's capture backend for this system: DirectShow on Windows (opens
    quickly), AVFoundation on macOS."""
    return cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_AVFOUNDATION


def available():
    """True if OpenCV is installed (camera snapshots possible)."""
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


def list_cameras(max_index=4):
    """Indexes of the cameras that open (0 is usually the built-in one).
    The system's own backend (DirectShow / AVFoundation) opens quickest."""
    import cv2
    found = []
    for i in range(max_index):
        cap = cv2.VideoCapture(i, _backend(cv2))
        if cap.isOpened():
            found.append(i)
        cap.release()
    return found


def snapshot(index=0, width=1280):
    """JPEG bytes of one frame from camera `index`. Raises RuntimeError."""
    try:
        import cv2
    except ImportError as e:
        raise RuntimeError("Camera support isn't installed.") from e
    cap = cv2.VideoCapture(int(index), _backend(cv2))
    try:
        if not cap.isOpened():
            raise RuntimeError(f"Camera {index} isn't available.")
        frame = None
        for _ in range(8):                      # let exposure settle
            ok, f = cap.read()
            if ok:
                frame = f
        if frame is None:
            raise RuntimeError("The camera didn't return a picture.")
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, int(h * width / w)))
        ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            raise RuntimeError("Couldn't encode the picture.")
        return buf.tobytes()
    finally:
        cap.release()
