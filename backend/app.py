import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import threading
import time
import uuid
import urllib.error
import urllib.request
import cv2
import numpy as np
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from .config import ConfigStore, Settings
from .camera_controls import CONTROL_KEYS
from .runtime import Runtime
from .storage import Database, as_csv
from .replay import ReplayJobs, analyze

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('DPM_DATA', ROOT/'data')).resolve()
DATA.mkdir(parents=True, exist_ok=True)
MEDIA = DATA/'media'
MEDIA.mkdir(exist_ok=True)
logger = logging.getLogger('dpm')
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(DATA/'scanner.log', maxBytes=2*1024*1024, backupCount=3)
handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
logger.addHandler(handler)
config = ConfigStore(DATA)
db = Database(DATA/'lab.sqlite3')
runtime = Runtime(config, db, MEDIA)
jobs = ReplayJobs()
offline_lock = threading.Lock()
AUTH_TOKEN = os.environ.get('DPM_TOKEN', '')
AUTH_COOKIE = secrets.token_urlsafe(32)


@asynccontextmanager
async def lifespan(app):
    runtime.start_worker()
    yield
    runtime.close()


app = FastAPI(title='DPM Scanner Lab', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware('http')
async def access(request: Request, call_next):
    if request.url.path.startswith(('/api/', '/media/')) and request.url.path not in ('/api/login','/api/health'):
        if AUTH_TOKEN and not hmac.compare_digest(request.cookies.get('dpm_session',''), AUTH_COOKIE):
            return JSONResponse({'detail':'Sign in with the scanner lab access token'}, status_code=401)
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin')
            if origin and origin != str(request.base_url).rstrip('/') and not (origin in ('http://127.0.0.1:5173','http://localhost:5173') and request.client.host in ('127.0.0.1','::1')):
                return JSONResponse({'detail':'Cross-origin writes are not allowed'}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def invalid_value(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=422)


@app.exception_handler(RuntimeError)
async def runtime_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=409)


def media_path(name):
    if not re.fullmatch(r'[a-f0-9]{32}\.(jpg|jpeg|png|bmp|avi|mp4|mov|mkv)', name):
        raise HTTPException(404, 'Media not found')
    p = MEDIA/name
    if not p.is_file():
        raise HTTPException(404, 'Media not found')
    return p


@app.get('/api/health')
def health():
    return {'service':'DPM Scanner Lab', 'version':'0.1.0', 'auth_required':bool(AUTH_TOKEN)}


@app.post('/api/login')
async def login(request: Request):
    body = await request.json()
    if AUTH_TOKEN and not hmac.compare_digest(str(body.get('token','')), AUTH_TOKEN):
        raise HTTPException(401, 'Invalid lab access token')
    response = JSONResponse({'ok':True})
    response.set_cookie('dpm_session', AUTH_COOKIE, httponly=True, samesite='strict', max_age=8*3600)
    return response


@app.get('/api/status')
def status():
    return runtime.status()


@app.get('/api/settings')
def settings():
    return config.settings.model_dump()


def control_service(values=None):
    request = urllib.request.Request('http://127.0.0.1:8096/'+('controls' if values is not None else 'status'),
        data=json.dumps(values).encode() if values is not None else None,
        headers={'Content-Type': 'application/json'}, method='POST' if values is not None else 'GET')
    try:
        with urllib.request.urlopen(request, timeout=8 if values is not None else 2) as response:
            return json.load(response)
    except (OSError, ValueError) as error:
        raise RuntimeError('Live camera controls are unavailable; check the Jetson camera service') from error


@app.get('/api/camera-controls')
def camera_controls_status():
    try:
        return control_service()
    except RuntimeError as error:
        return {'available': False, 'error': str(error), 'focus_control': 'manual lens adjustment'}


@app.get('/api/export/calibration')
def export_calibration():
    report = {'schema': 'dpm-calibration-v1', 'created_epoch': time.time(),
              'settings': config.settings.model_dump(), 'camera': camera_controls_status(),
              'focus': 'Manual lens adjustment; focus_distance_mm records the test distance',
              'exposure_units': 'microseconds; camera properties are requested limits, not measured exposure'}
    return Response(json.dumps(report, indent=2), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="dpm-iotiview-calibration.json"'})


@app.patch('/api/settings')
def update_settings(data: dict):
    with runtime.lock:
        hardware = {'adapter','device','sensor_mode','width','height','fps'}
        if any(k in data and data[k]!=getattr(config.settings,k) for k in hardware) and runtime.camera.state not in ('STOPPED','ERROR'):
            raise RuntimeError('Stop the camera before changing its adapter, resolution or frame rate')
        prepared = Settings(**{**config.settings.model_dump(), **data})
        if prepared.adapter == 'v4l2' and prepared.device == 10 and any(getattr(prepared, key) != getattr(config.settings, key) for key in ('width','height','fps')):
            if camera_controls_status().get('available') and (prepared.width, prepared.height, prepared.fps) != (1920,1080,30):
                raise RuntimeError('This camera feed uses 1920×1080 at a requested 30 fps; adjust exposure controls without changing its capture format')
        camera_changed = any(getattr(prepared, key) != getattr(config.settings, key) for key in CONTROL_KEYS)
        if camera_changed:
            if prepared.adapter != 'v4l2' or prepared.device != 10:
                raise RuntimeError('Use the Jetson virtual webcam on device 10 for live exposure controls')
            control_service({key: getattr(prepared, key) for key in CONTROL_KEYS})
        result = config.update(data)
        runtime.pipeline.reset()
        runtime.reset_reads()
        return result.model_dump()


@app.get('/api/presets')
def presets():
    return config.presets()


class PresetName(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.post('/api/presets')
def save_preset(data: PresetName):
    with runtime.lock:
        config.save_preset(data.name)
    return config.presets()


@app.post('/api/camera/{action}')
def camera_action(action: str):
    with runtime.lock:
        if action == 'start':
            if runtime.camera.state in ('STOPPED','ERROR'):
                runtime.latest_scan = None
                runtime.pipeline.reset()
                runtime.reset_reads()
                runtime.paused = False
            runtime.camera.start(config.settings)
        elif action == 'stop':
            runtime.camera.stop()
            runtime.raw = runtime.jpeg = runtime.report = None
            runtime.pipeline.reset()
            runtime.reset_reads()
        elif action == 'pause':
            runtime.paused = True
            runtime.reset_reads()
        elif action == 'resume':
            runtime.paused = False
            runtime.pipeline.reset()
        elif action == 'reset-tracking':
            runtime.pipeline.reset()
            runtime.reset_reads()
        elif action == 'reset-session':
            runtime.pipeline.reset()
            runtime.scan_count = runtime.failed_attempts = runtime.drops = 0
            runtime.latest_scan = None
            runtime.reset_reads(reset_count=True)
        elif action in ('capture', 'diagnostic'):
            name = runtime.capture()
            if action == 'diagnostic':
                (MEDIA/(name+'.json')).write_text(json.dumps({'settings':config.settings.model_dump(),'status':runtime.status()},indent=2))
            return {'file': name}
        elif action == 'record':
            return {'file': runtime.record()}
        else:
            raise HTTPException(404, 'Unknown camera action')
    return runtime.status()


@app.post('/api/frame')
async def browser_frame(request: Request):
    data = await request.body()
    if len(data) > 4*1024*1024:
        raise HTTPException(413, 'Browser frame exceeds 4 MB')
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if image is None or image.shape[0]*image.shape[1] > 12_600_000:
        raise HTTPException(422, 'Invalid camera image')
    runtime.camera.submit(image)
    return {'ok':True}


@app.get('/api/stream')
async def stream():
    async def frames():
        last = None
        while True:
            jpeg = runtime.jpeg
            if jpeg and jpeg is not last:
                last = jpeg
                yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '+str(len(jpeg)).encode()+b'\r\n\r\n'+jpeg+b'\r\n'
            await asyncio.sleep(0.01)
    return StreamingResponse(frames(), media_type='multipart/x-mixed-replace; boundary=frame', headers={'Cache-Control':'no-store'})


@app.get('/api/scans')
def scans():
    return db.rows('scans',200)


@app.get('/api/scan-events')
async def scan_events(request: Request):
    async def events():
        previous = None
        heartbeat = time.monotonic()
        while not await request.is_disconnected():
            # Replaced atomically by the worker. Do not wait on native decoding.
            feedback = runtime.read_feedback
            if feedback is not previous:
                previous = feedback
                yield 'data: '+json.dumps(feedback)+'\n\n'
                heartbeat = time.monotonic()
            elif time.monotonic()-heartbeat >= 15:
                yield ': keepalive\n\n'
                heartbeat = time.monotonic()
            await asyncio.sleep(.05)
    return StreamingResponse(events(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


class SessionInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    notes: str = Field('', max_length=2000)


@app.get('/api/sessions')
def sessions():
    return db.rows('sessions')


@app.post('/api/sessions')
def create_session(data: SessionInput):
    runtime.session_id = db.session(data.name, {'notes':data.notes,'settings':config.settings.model_dump()})
    return {'id':runtime.session_id}


@app.post('/api/sessions/{item_id}/select')
def select_session(item_id: int):
    if not any(s['id']==item_id for s in db.rows('sessions',10000)):
        raise HTTPException(404,'Session not found')
    if runtime.trial:
        raise RuntimeError('Finish the active trial before switching sessions')
    runtime.session_id = item_id
    return {'id':item_id}


class TrialInput(BaseModel):
    expected: str = Field(min_length=1, max_length=128)
    preset: str = Field('Custom', max_length=60)
    distance: str = Field('Unspecified', max_length=100)
    angle: str = Field('', max_length=100)
    surface: str = Field('Unspecified', max_length=100)
    lighting: str = Field('Unspecified', max_length=100)
    notes: str = Field('', max_length=2000)
    timeout_seconds: int = Field(10, ge=2, le=60)


@app.post('/api/trials')
def begin_trial(data: TrialInput):
    if not runtime.session_id:
        raise RuntimeError('Create or select a test session first')
    if runtime.camera.state != 'RUNNING' or runtime.paused:
        raise RuntimeError('Start the camera and resume processing before beginning a live trial')
    runtime.begin_trial(data.model_dump())
    return {'ok':True}


@app.post('/api/trials/finish')
def finish_trial():
    return runtime.finish_trial('timeout', None)


@app.get('/api/analytics')
def analytics():
    trials = db.rows('trials',10000)
    correct = sum(t['outcome']=='correct' for t in trials)
    latencies = [t['latency_ms'] for t in trials if t['outcome']=='correct']
    groups = {}
    for key in ('preset','distance','surface','lighting'):
        buckets = {}
        for t in trials:
            v = t.get(key,'Unspecified')
            b = buckets.setdefault(v, {'tests':0,'correct':0,'incorrect':0,'timeouts':0})
            b['tests'] += 1
            b['correct'] += t['outcome']=='correct'
            b['incorrect'] += t['outcome']=='incorrect'
            b['timeouts'] += t['outcome']=='timeout'
        groups[key] = buckets
    return {'trials':trials, 'total':len(trials),'correct':correct,'incorrect':sum(t['outcome']=='incorrect' for t in trials),
            'timeouts':sum(t['outcome']=='timeout' for t in trials), 'success_rate':correct/len(trials) if trials else None,
            'mean_latency_ms': float(np.mean(latencies)) if latencies else None,
            'p95_latency_ms': float(np.percentile(latencies,95)) if latencies else None,
            'groups':groups,'samples':db.rows('samples',120)[::-1]}


@app.get('/api/export/{kind}')
def export(kind: str, format: str = 'json', session_id: int | None = None):
    if kind not in ('scans','trials','sessions') or format not in ('json','csv'):
        raise HTTPException(400,'Choose scans, trials or sessions; JSON or CSV')
    rows = db.rows(kind,100000,session_id)
    content = as_csv(rows) if format=='csv' else json.dumps(rows,indent=2)
    return Response(content,media_type='text/csv' if format=='csv' else 'application/json',
                    headers={'Content-Disposition':f'attachment; filename="dpm-{kind}.{format}"'})


@app.get('/api/media')
def media():
    return [{'name':p.name,'bytes':p.stat().st_size,'modified':p.stat().st_mtime} for p in sorted(MEDIA.iterdir(),key=lambda p:p.stat().st_mtime,reverse=True) if p.suffix.lower() in ('.jpg','.jpeg','.png','.bmp','.avi','.mp4','.mov','.mkv')]


@app.post('/api/media')
async def upload(file: UploadFile = File(...)):
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in ('.jpg','.jpeg','.png','.bmp','.avi','.mp4','.mov','.mkv'):
        raise HTTPException(422,'Upload a JPG, PNG, BMP, MP4, AVI, MOV or MKV file')
    name = uuid.uuid4().hex+suffix
    target = MEDIA/name
    count = 0
    try:
        with target.open('wb') as out:
            while chunk := await file.read(1024*1024):
                count += len(chunk)
                if count > 100*1024*1024:
                    raise HTTPException(413,'Upload limit is 100 MB per file')
                runtime.storage_guard(len(chunk))
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    return {'name':name,'bytes':count}


@app.delete('/api/media/{name}')
def delete_media(name: str):
    if runtime.recording and name==runtime.record_name or jobs.gate.locked():
        raise RuntimeError('Wait for recording and offline jobs to finish before removing media')
    media_path(name).unlink()
    (MEDIA/(name+'.json')).unlink(missing_ok=True)
    return {'ok':True}


@app.get('/media/{name}')
def media_file(name: str):
    return FileResponse(media_path(name))


@app.get('/api/analyze/{name}')
def analyze_file(name: str, frame: int=0):
    if frame<0:
        raise HTTPException(422,'Frame index must be positive')
    if not offline_lock.acquire(blocking=False):
        raise RuntimeError('An image analysis is already running')
    try:
        return analyze(media_path(name),config.settings,frame)
    finally:
        offline_lock.release()


class ReplayInput(BaseModel):
    files: list[str] = Field(min_length=1, max_length=20)
    presets: list[str] = Field(default_factory=list, max_length=2)
    expected: str = Field('',max_length=128)
    label: str = Field('',max_length=100)


@app.post('/api/replay')
def start_replay(data: ReplayInput):
    choices = config.presets()
    configs = [(name,Settings(**choices[name])) for name in data.presets if name in choices]
    if data.presets and len(configs)!=len(data.presets):
        raise HTTPException(422,'Unknown preset')
    if not configs:
        configs = [('Current',config.settings)]
    key = jobs.start([media_path(n) for n in data.files],configs,data.expected,{'label':data.label})
    return {'id':key}


@app.get('/api/replay/{key}')
def replay_job(key: str):
    if key not in jobs.jobs:
        raise HTTPException(404,'Replay job not found')
    return jobs.jobs[key]


class HolderInput(BaseModel):
    tool_id: str = Field(min_length=1,max_length=100)
    holder_id: str = Field(min_length=1,max_length=128)
    part: str = Field('',max_length=100)
    location: str = Field('Lab',max_length=100)
    building: str = Field('Lab',max_length=100)
    dimensions: str = Field('',max_length=100)
    life: str = Field('',max_length=100)
    status: str = Field('In Crib',pattern='^(Installed|In Crib|Discarded)$')


@app.get('/api/holders')
def holders():
    return db.rows('holders',10000)


@app.post('/api/holders')
def add_holder(data: HolderInput):
    item_id = db.add('holders',{**data.model_dump(),'created_epoch':time.time()})
    return {'id':item_id}


@app.delete('/api/holders/{item_id}')
def remove_holder(item_id: int):
    db.remove_holder(item_id)
    return {'ok':True}


if (ROOT/'dist').is_dir():
    app.mount('/',StaticFiles(directory=ROOT/'dist',html=True),name='frontend')
