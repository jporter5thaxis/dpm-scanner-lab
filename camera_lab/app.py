"""Single-station camera comparison lab. Run from the project root with uvicorn."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
import uuid

import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from backend.camera import CameraManager, v4l2_loopback
from backend.vision import quality

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CAMERA_LAB_DATA', ROOT.parent / 'data' / 'camera-quality'))
DATA.mkdir(parents=True, exist_ok=True)
reader = CameraManager()
lock = threading.RLock()
selection = None
session = None


def device_modes(node):
    output = subprocess.run(['v4l2-ctl', '-d', str(node), '--list-formats-ext'],
                            capture_output=True, text=True, timeout=3)
    modes = []
    fmt = size = None
    for line in output.stdout.splitlines():
        match = re.search(r"\[\d+\]: '(.{4})'", line)
        if match:
            fmt, size = match[1], None
        match = re.search(r'Size: Discrete (\d+)x(\d+)', line)
        if match:
            size = [int(match[1]), int(match[2])]
        match = re.search(r'\(([\d.]+) fps\)', line)
        if match and size and fmt in ('MJPG', 'YUYV', 'UYVY', 'NV12', 'RGB3', 'BGR3'):
            modes.append(dict(fourcc=fmt, width=size[0], height=size[1], fps=float(match[1])))
    return modes


def devices():
    found = []
    for entry in sorted(Path('/sys/class/video4linux').glob('video[0-9]*')):
        try:
            name = (entry / 'name').read_text().strip()
            number = int(entry.name[5:])
            driver = (entry / 'device/driver').resolve().name
            if driver != 'uvcvideo' and not v4l2_loopback(number):
                continue
            modes = device_modes(Path('/dev') / entry.name)
            if modes:
                found.append(dict(device=number, name=name, modes=modes))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            continue
    return found


@asynccontextmanager
async def lifespan(app):
    yield
    reader.stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def local_origin(request: Request, call_next):
    if request.method == 'POST' and request.headers.get('origin') not in (None, str(request.base_url).rstrip('/')):
        return JSONResponse({'detail': 'Use this station’s camera lab.'}, status_code=403)
    return await call_next(request)


class Start(BaseModel):
    device: int
    name: str
    fourcc: str
    width: int
    height: int
    fps: float


@app.get('/api/devices')
def list_devices():
    return devices()


@app.post('/api/start')
def start(req: Start):
    global selection, session
    with lock:
        mode = req.model_dump(exclude={'device', 'name'})
        if not any(d['device'] == req.device and d['name'] == req.name and mode in d['modes'] for d in devices()):
            raise HTTPException(409, 'The camera or mode changed. Refresh devices.')
        reader.stop()
        if reader.worker and reader.worker.is_alive():
            raise HTTPException(409, 'Previous camera is still releasing.')
        selection, session = req.model_dump(), uuid.uuid4().hex
        reader.start(SimpleNamespace(adapter='v4l2', **selection))
        return dict(session=session)


@app.post('/api/stop')
def stop():
    global session, selection
    with lock:
        session = selection = None
        reader.stop()
    return {'stopped': True}


@app.get('/api/status')
def status():
    with lock:
        return dict(camera=reader.status(), selection=selection, session=session)


@app.get('/api/preview/{key}')
async def preview(key: str):
    if key != session:
        raise HTTPException(409, 'Camera selection changed')
    async def stream():
        last = None
        while key == session:
            frame = reader.snapshot()
            if frame and frame.sequence != last and time.monotonic() - frame.captured < 2:
                last = frame.sequence
                def encode(image):
                    h, w = image.shape[:2]
                    if w > 960:
                        image = cv2.resize(image, (960, max(1, round(h*960/w))), interpolation=cv2.INTER_AREA)
                    ok, jpg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    return jpg.tobytes() if ok else b''
                jpg = await asyncio.to_thread(encode, frame.image)
                if jpg and key == session:
                    yield b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + jpg + b'\r\n'
            await asyncio.sleep(.04)
    return StreamingResponse(stream(), media_type='multipart/x-mixed-replace; boundary=frame', headers={'Cache-Control':'no-store'})


class Shot(BaseModel):
    session: str
    note: str = Field(default='', max_length=160)


@app.post('/api/shots')
def capture(req: Shot):
    with lock:
        frame = reader.snapshot()
        if req.session != session or not selection or reader.state != 'RUNNING' or not frame or time.monotonic()-frame.captured > 2:
            raise HTTPException(409, 'Wait for a fresh frame from the selected camera.')
        return save_frame(frame.image, selection['name'], selection.copy(), reader.fps, req.note)


@app.post('/api/browser-shot')
async def browser_shot(request: Request, camera: str = Query(default='Browser camera', max_length=256),
                       note: str = Query(default='', max_length=160), fps: float = Query(default=0, ge=0, le=1000, allow_inf_nan=False)):
    if request.headers.get('content-type') != 'image/png':
        raise HTTPException(415, 'Expected a PNG frame.')
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 160 * 1024**2:
            raise HTTPException(413, 'Camera frame is too large.')
        data.extend(chunk)
    if data[:8] != bytes.fromhex('89504e470d0a1a0a') or data[12:16] != b'IHDR':
        raise HTTPException(422, 'Invalid PNG frame.')
    width, height = (int.from_bytes(data[i:i+4], 'big') for i in (16, 20))
    if not 0 < width * height <= 60_000_000:
        raise HTTPException(413, 'Camera frame dimensions exceed the supported range.')
    def store():
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise HTTPException(422, 'Could not read the camera frame.')
        return save_frame(image, camera, {'source': 'browser'}, fps, note)
    return await asyncio.to_thread(store)


def save_frame(image, camera, requested, fps, note):
    if shutil.disk_usage(DATA).free < 512 * 1024**2:
        raise HTTPException(507, 'Storage is low. Archive captures before continuing.')
    h, w = image.shape[:2]
    key = uuid.uuid4().hex
    item = dict(id=key, camera=camera, requested=requested, width=w, height=h,
                fps=fps, captured=datetime.now(timezone.utc).isoformat(), note=note)
    # All metrics use the same displayed width so resolution alone doesn't set the scale.
    small = cv2.resize(image, (min(960,w), max(1,round(h*min(960,w)/w))), interpolation=cv2.INTER_AREA)
    item.update(quality(small))
    crop = image[max(0,(h-256)//2):(h+256)//2, max(0,(w-256)//2):(w+256)//2]
    tmp = DATA / ('.'+key)
    tmp.mkdir()
    try:
        for name, pixels in [('original.png', image), ('detail.png', crop), ('preview.jpg', small)]:
            if not cv2.imwrite(str(tmp/name), pixels):
                raise OSError('Unable to write capture')
        (tmp/'shot.json').write_text(json.dumps(item))
        tmp.rename(DATA/key)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return item



@app.get('/api/shots')
def shots():
    items = [json.loads(meta.read_text()) for meta in DATA.glob('[!.]*/shot.json')]
    return sorted(items, key=lambda item:item['captured'], reverse=True)


@app.get('/api/shots/{key}/{name}')
def shot_file(key: str, name: str):
    if not re.fullmatch('[0-9a-f]{32}', key) or name not in ('original.png','detail.png','preview.jpg','shot.json') or not (DATA/key/name).is_file():
        raise HTTPException(404)
    return FileResponse(DATA/key/name)


app.mount('/', StaticFiles(directory=ROOT/'web', html=True))
