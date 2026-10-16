"""One capture reader. Latest-frame delivery; hardware-specific details stay here."""
from dataclasses import dataclass
import os
import platform
import queue
import shutil
import subprocess
from collections import deque
import threading
import time
from typing import Protocol
import cv2
import numpy as np
from .config import Settings


@dataclass
class Frame:
    image: np.ndarray
    sequence: int
    captured: float


class Adapter(Protocol):
    def open(self): ...
    def read(self): ...
    def close(self): ...


class BrowserAdapter:
    def __init__(self, cfg):
        self.frames = queue.Queue(maxsize=1)

    def open(self):
        pass

    def submit(self, image):
        try:
            self.frames.get_nowait()
        except queue.Empty:
            pass
        self.frames.put_nowait(image)

    def read(self):
        try:
            return True, self.frames.get(timeout=0.5)
        except queue.Empty:
            return True, None

    def close(self):
        pass


def v4l2_loopback(device):
    """Identify producer-owned virtual cameras with a read-only capability query."""
    if platform.system() != 'Linux':
        return False
    import fcntl
    try:
        descriptor = os.open(f'/dev/video{device}', os.O_RDONLY | os.O_NONBLOCK)
        try:
            capabilities = bytearray(104)
            # VIDIOC_QUERYCAP, struct v4l2_capability (Linux ARM64/x86 ABI).
            fcntl.ioctl(descriptor, 0x80685600, capabilities, True)
            driver = bytes(capabilities[:16]).split(b'\0', 1)[0]
            return driver in (b'v4l2 loopback', b'v4l2loopback')
        finally:
            os.close(descriptor)
    except OSError:
        return False


class OpenCVAdapter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.capture = None

    def open(self):
        cfg = self.cfg
        if cfg.adapter == 'csi':
            if 'GStreamer:                   YES' not in cv2.getBuildInformation() and not any('GStreamer' in line and 'YES' in line for line in cv2.getBuildInformation().splitlines()):
                raise RuntimeError('CSI needs Jetson OpenCV with GStreamer enabled. The local pip OpenCV build does not support it.')
            mode = f'sensor-mode={cfg.sensor_mode}' if cfg.sensor_mode >= 0 else ''
            pipeline = (f'nvarguscamerasrc sensor-id={cfg.device} {mode} ! video/x-raw(memory:NVMM), '
                        f'width=(int){cfg.width}, height=(int){cfg.height}, framerate=(fraction){cfg.fps}/1 ! '
                        'nvvidconv ! video/x-raw, format=(string)BGRx ! videoconvert ! video/x-raw, format=(string)BGR ! '
                        'appsink max-buffers=1 drop=true sync=false')
            self.capture = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        else:
            producer_owned = v4l2_loopback(cfg.device)
            self.capture = cv2.VideoCapture(cfg.device, cv2.CAP_V4L2 if platform.system() == 'Linux' else cv2.CAP_DSHOW)
            # Renegotiating a loopback consumer can invalidate the live producer's
            # buffers. Its existing service owns the format and frame rate.
            if not producer_owned:
                self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.width)
                self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.height)
                self.capture.set(cv2.CAP_PROP_FPS, cfg.fps)
                self.capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not self.capture.isOpened():
            self.close()
            raise RuntimeError('Camera could not open. Check the selected adapter, device, sensor mode and camera availability.')

    def read(self):
        return self.capture.read()

    def close(self):
        if self.capture is not None:
            self.capture.release()
            self.capture = None


def opencv_gstreamer():
    return any('GStreamer' in line and 'YES' in line for line in cv2.getBuildInformation().splitlines())


class GStreamerCSIAdapter:
    """Use installed Jetson GStreamer when the isolated OpenCV wheel lacks it.

    GStreamer writes raw BGR to a pipe. A dedicated bounded reader discards stale
    frames; the capture manager can stop even if a camera driver stops returning.
    """
    def __init__(self,cfg):
        self.cfg=cfg
        self.process=None
        self.frames=queue.Queue(maxsize=1)
        self.errors=deque(maxlen=12)
        self.started=0
        self.last_frame=0

    def open(self):
        executable=shutil.which('gst-launch-1.0')
        if not executable:
            raise RuntimeError('CSI requires the Jetson GStreamer runtime')
        cfg=self.cfg
        args=[executable,'-q','nvarguscamerasrc',f'sensor-id={cfg.device}']
        if cfg.sensor_mode>=0:
            args.append(f'sensor-mode={cfg.sensor_mode}')
        args.extend(['!','video/x-raw(memory:NVMM),'+f'width=(int){cfg.width},height=(int){cfg.height},framerate=(fraction){cfg.fps}/1',
                     '!','nvvidconv','!','video/x-raw,format=(string)BGRx','!','videoconvert','!',
                     'video/x-raw,format=(string)BGR','!','fdsink','fd=1','sync=false'])
        self.process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,bufsize=0)
        self.started=self.last_frame=time.monotonic()
        threading.Thread(target=self._read_frames,daemon=True,name='gstreamer-raw-reader').start()
        threading.Thread(target=self._read_errors,daemon=True,name='gstreamer-error-reader').start()

    def _read_errors(self):
        for line in self.process.stderr:
            self.errors.append(line.decode(errors='replace').strip()[:400])

    def _read_frames(self):
        # GStreamer BGR rows use four-byte alignment.
        stride=(self.cfg.width*3+3)//4*4
        size=stride*self.cfg.height
        while self.process.poll() is None:
            data=bytearray()
            while len(data)<size:
                block=self.process.stdout.read(size-len(data))
                if not block:
                    return
                data.extend(block)
            rows=np.frombuffer(data,np.uint8).reshape(self.cfg.height,stride)
            image=rows[:,:self.cfg.width*3].reshape(self.cfg.height,self.cfg.width,3).copy()
            try:
                self.frames.get_nowait()
            except queue.Empty:
                pass
            self.frames.put_nowait(image)

    def read(self):
        try:
            image=self.frames.get(timeout=.5)
            self.last_frame=time.monotonic()
            return True,image
        except queue.Empty:
            if self.process.poll() is not None:
                raise RuntimeError('CSI capture exited: '+(' | '.join(self.errors)[-1200:] or 'No camera frames returned'))
            if time.monotonic()-self.last_frame>10:
                raise RuntimeError('CSI camera did not deliver a frame within ten seconds')
            return True,None

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process.stdout.close()
            self.process.stderr.close()


class CameraManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker = None
        self.adapter = None
        self.latest = None
        self.sequence = 0
        self.state = 'STOPPED'
        self.error = None
        self.fps = 0.0
        self.config = None

    def start(self, cfg):
        with self.lock:
            if self.worker and self.worker.is_alive():
                if self.state == 'STOPPING':
                    raise RuntimeError('Camera is still stopping; wait before restarting')
                return
            self.config = cfg
            self.latest = None
            self.error = None
            self.stop_event.clear()
            self.state = 'STARTING'
            self.worker = threading.Thread(target=self._run, daemon=True, name='camera-capture')
            self.worker.start()

    def _run(self):
        retries = 0
        ticks = []
        try:
            while not self.stop_event.is_set():
                self.adapter = (BrowserAdapter(self.config) if self.config.adapter == 'browser' else
                                GStreamerCSIAdapter(self.config) if self.config.adapter == 'csi' and not opencv_gstreamer() else
                                OpenCVAdapter(self.config))
                try:
                    self.adapter.open()
                    self.state = 'RUNNING'
                    self.error = None
                    while not self.stop_event.is_set():
                        ok, image = self.adapter.read()
                        if not ok:
                            raise RuntimeError('Camera capture failed or device disconnected')
                        if image is None:
                            continue
                        retries = 0
                        now = time.monotonic()
                        ticks = [t for t in ticks if now - t < 2]
                        ticks.append(now)
                        self.fps = round((len(ticks)-1)/(ticks[-1]-ticks[0]), 1) if len(ticks)>1 else 0
                        with self.lock:
                            self.sequence += 1
                            self.latest = Frame(image, self.sequence, now)
                except Exception as exc:
                    self.error = str(exc)
                    self.state = 'ERROR'
                    retries += 1
                    if retries >= 3 or self.stop_event.wait(min(retries * 2, 6)):
                        break
                    self.state = 'RECOVERING'
                finally:
                    self.adapter.close()
        finally:
            if self.stop_event.is_set():
                self.state = 'STOPPED'
            self.fps = 0

    def submit(self, image):
        if not isinstance(self.adapter, BrowserAdapter) or self.state != 'RUNNING':
            raise RuntimeError('Browser camera is not running')
        self.adapter.submit(image)

    def snapshot(self):
        with self.lock:
            return self.latest

    def stop(self):
        self.stop_event.set()
        self.state = 'STOPPING' if self.worker and self.worker.is_alive() else 'STOPPED'
        if self.worker:
            self.worker.join(timeout=3)
            if self.worker.is_alive():
                self.error = 'Capture reader is waiting for the driver to return. Restart is disabled until it exits.'
            else:
                self.state = 'STOPPED'
        with self.lock:
            self.latest = None

    def status(self):
        frame = self.snapshot()
        fresh = frame is not None and time.monotonic()-frame.captured < 2
        return {'state': self.state, 'error': self.error, 'fps': self.fps if fresh else 0,
                'resolution': [frame.image.shape[1], frame.image.shape[0]] if frame else None,
                'age_ms': round((time.monotonic()-frame.captured)*1000) if frame else None,
                'adapter': self.config.adapter if self.config else None}
