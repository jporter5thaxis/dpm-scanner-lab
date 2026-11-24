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
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from backend.camera import CameraManager
from backend.vision import quality

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('CAMERA_LAB_DATA', ROOT.parent / 'data' / 'camera-quality'))
DATA.mkdir(parents=True, exist_ok=True)
reader = CameraManager()
lock = threading.RLock()
selection = None
session = None


def device_modes(formats):
    modes = []
    fmt = size = None
    for line in formats.splitlines():
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


def query_node(node, option):
    result = subprocess.run(['v4l2-ctl', '-d', str(node), option], capture_output=True,
                            text=True, errors='replace', timeout=3)
    if result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or 'Device query failed.')
    return result.stdout


def inspect_nodes(include_controls=False):
    nodes = []
    for node in sorted(Path('/dev').glob('video[0-9]*')):
        if not re.fullmatch(r'video[0-9]+', node.name):
            continue
        item = dict(device=int(node.name[5:]), path=str(node), modes=[])
        nodes.append(item)
        try:
            info = query_node(node, '--info')
            fields = {k.strip(): v.strip() for line in info.splitlines() if ':' in line
                      for k, v in [line.split(':', 1)]}
            item.update(driver=fields.get('Driver name', ''), name=fields.get('Card type', node.name),
                        bus=fields.get('Bus info', ''))
            if item['driver'] not in ('uvcvideo', 'v4l2 loopback', 'v4l2loopback'):
                item['issue'] = 'This node requires a USB webcam or virtual webcam driver.'
                continue
            formats = query_node(node, '--list-formats-ext')
            item['modes'] = device_modes(formats)
            if not item['modes']:
                item['issue'] = 'No supported capture modes. Check for another video node on this camera.'
            if include_controls:
                item['formats'] = formats.strip()
                item['controls'] = query_node(node, '--list-ctrls-menus').strip()
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            item['issue'] = str(exc)
    return nodes


def devices():
    return [node for node in inspect_nodes() if node['modes']]


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


@app.get('/api/driver-report')
def driver_report():
    return {'nodes': inspect_nodes(include_controls=True)}


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
