import threading
import io
import numpy as np
import pytest
import backend.camera as module
from backend.camera import BrowserAdapter, CameraManager
from backend.config import Settings


@pytest.mark.parametrize('producer_owned', [True, False])
def test_virtual_camera_preserves_producer_format(monkeypatch, producer_owned):
    changes = []
    class Capture:
        def set(self, key, value):
            changes.append((key, value))
        def isOpened(self):
            return True
        def release(self):
            pass
    monkeypatch.setattr(module, 'v4l2_loopback', lambda device: producer_owned)
    monkeypatch.setattr(module.cv2, 'VideoCapture', lambda *args: Capture())
    cfg = Settings(adapter='v4l2', device=10)
    adapter = module.OpenCVAdapter(cfg)
    adapter.open()
    assert changes == ([] if producer_owned else [
        (module.cv2.CAP_PROP_FRAME_WIDTH, cfg.width),
        (module.cv2.CAP_PROP_FRAME_HEIGHT, cfg.height),
        (module.cv2.CAP_PROP_FPS, cfg.fps),
        (module.cv2.CAP_PROP_BUFFERSIZE, 1),
    ])
    adapter.close()


def test_latest_browser_frame_replaces_stale_frames():
    adapter=BrowserAdapter(Settings())
    adapter.submit(np.zeros((10,10,3),np.uint8))
    fresh=np.ones((10,10,3),np.uint8)
    adapter.submit(fresh)
    assert adapter.frames.qsize()==1
    assert adapter.read()[1] is fresh


def test_camera_recovers_then_releases_adapter(monkeypatch):
    camera=CameraManager()
    opened=[]
    closed=[]
    class FakeAdapter:
        def __init__(self,cfg):self.attempt=len(opened)
        def open(self):opened.append(self)
        def read(self):
            if self.attempt==0:return False,None
            camera.stop_event.set()
            return True,np.zeros((10,20,3),np.uint8)
        def close(self):closed.append(self)
    monkeypatch.setattr(module,'OpenCVAdapter',FakeAdapter)
    monkeypatch.setattr(camera.stop_event,'wait',lambda seconds:False)
    camera.config=Settings(adapter='v4l2')
    camera._run()
    assert len(opened)==len(closed)==2
    assert camera.state=='STOPPED'
    assert camera.snapshot().image.shape==(10,20,3)


def test_start_is_idempotent_and_stop_releases():
    camera=CameraManager()
    camera.start(Settings())
    reader=camera.worker
    camera.start(Settings())
    assert camera.worker is reader
    camera.stop()
    assert not reader.is_alive()
    assert camera.state=='STOPPED' and camera.latest is None


def test_gstreamer_pipe_frame_stride_and_cleanup(monkeypatch):
    cfg=Settings(adapter='csi',width=321,height=240)
    stride=(cfg.width*3+3)//4*4
    raw=np.zeros((cfg.height,stride),np.uint8)
    raw[:,:cfg.width*3]=123
    launches=[]
    class Process:
        def __init__(self,args,**kwargs):
            launches.append((args,kwargs))
            self.stdout=io.BytesIO(raw.tobytes())
            self.stderr=io.BytesIO()
            self.code=None
        def poll(self):return self.code
        def terminate(self):self.code=0
        def wait(self,timeout):return self.code
        def kill(self):self.code=-9
    monkeypatch.setattr(module.shutil,'which',lambda name:'/usr/bin/gst-launch-1.0')
    monkeypatch.setattr(module.subprocess,'Popen',Process)
    adapter=module.GStreamerCSIAdapter(cfg)
    adapter.open()
    ok,image=adapter.read()
    assert ok and image.shape==(240,321,3) and np.all(image==123)
    assert launches[0][0][0]=='/usr/bin/gst-launch-1.0'
    assert 'shell' not in launches[0][1]
    adapter.close()
    assert adapter.process.poll()==0
