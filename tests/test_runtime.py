import time
import cv2
import numpy as np
from backend.camera import Frame
from backend.config import ConfigStore
from backend.runtime import Runtime
from backend.storage import Database


def wait_until(predicate):
    deadline = time.monotonic()+2
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Preview did not update independently of processing')


def test_preview_stays_live_while_processing_lock_is_held(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), Database(tmp_path/'test.sqlite3'), tmp_path)
    runtime.camera.state = 'RUNNING'
    original = np.zeros((1080,1920,3), np.uint8)
    try:
        with runtime.lock:
            runtime.camera.latest = Frame(original, 1, time.monotonic())
            runtime.start_worker()
            wait_until(lambda: runtime.jpeg is not None)
            first = runtime.jpeg
            runtime.camera.latest = Frame(np.full_like(original, 255), 2, time.monotonic())
            wait_until(lambda: runtime.jpeg is not first)
            image = cv2.imdecode(np.frombuffer(runtime.jpeg, np.uint8), cv2.IMREAD_COLOR)
            assert image.shape[:2] == (540,960) and image.mean() > 250
            assert runtime.camera.latest.image.shape[:2] == (1080,1920)
            assert runtime.report is None
    finally:
        runtime.close()
    assert not runtime.preview_worker.is_alive()
