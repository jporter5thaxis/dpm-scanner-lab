"""Keep one IMX219 producer open and apply only validated capture properties."""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import gi
gi.require_version('Gst', '1.0')
from gi.repository import GLib, Gst
from camera_controls import DEFAULT_CONTROLS, CONTROL_KEYS, property_requests, validate_controls

Gst.init(None)
pipeline = Gst.parse_launch('nvarguscamerasrc name=sensor sensor-id=0 sensor-mode=2 ! '
    'video/x-raw(memory:NVMM),width=1920,height=1080,format=NV12,framerate=30/1 ! '
    'nvvidconv flip-method=6 ! video/x-raw,format=I420,width=1920,height=1080,framerate=30/1 ! '
    'videoconvert ! video/x-raw,format=YUY2,width=1920,height=1080,framerate=30/1 ! '
    'identity drop-allocation=true ! v4l2sink device=/dev/video10 sync=false')
sensor = pipeline.get_by_name('sensor')
loop = GLib.MainLoop()
lock = threading.Lock()
frames = deque(maxlen=90)
state = {'available': True, 'controls': dict(DEFAULT_CONTROLS), 'property_requests': {},
         'error': None, 'sensor': 'IMX219', 'width': 1920, 'height': 1080, 'fps': 30,
         'focus_control': 'manual lens adjustment', 'frames_received': 0}


def apply(values):
    values = validate_controls(values)
    properties = property_requests(values)
    for key, value in properties.items():
        if sensor.find_property(key) is None:
            raise ValueError('Camera property is unavailable: '+key)
        sensor.set_property(key, value)
    with lock:
        state.update(controls=values, property_requests=properties, error=None)


def frame_seen(pad, info):
    with lock:
        frames.append(time.monotonic())
        state['frames_received'] += 1
    return Gst.PadProbeReturn.OK


sensor.get_static_pad('src').add_probe(Gst.PadProbeType.BUFFER, frame_seen)


def status():
    with lock:
        result = dict(state)
        result['frame_age_ms'] = round((time.monotonic()-frames[-1])*1000) if frames else None
        recent = [stamp for stamp in frames if time.monotonic()-stamp < 2]
        result['measured_fps'] = round((len(recent)-1)/(recent[-1]-recent[0]), 1) if len(recent)>1 else 0
    return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, code, value):
        encoded = json.dumps(value).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        self.respond(200, status()) if self.path == '/status' else self.respond(404, {'detail': 'Unknown route'})

    def do_POST(self):
        if self.path != '/controls':
            return self.respond(404, {'detail': 'Unknown route'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                raise ValueError('Invalid control request size')
            values = validate_controls(json.loads(self.rfile.read(length)))
        except (ValueError, TypeError):
            return self.respond(422, {'detail': 'Invalid camera controls'})
        done = threading.Event()
        outcome = {}
        def update():
            try:
                apply(values)
            except Exception as error:
                outcome['error'] = str(error)
            finally:
                done.set()
            return False
        GLib.idle_add(update)
        if not done.wait(6):
            return self.respond(503, {'detail': 'Camera control update timed out'})
        if outcome.get('error'):
            return self.respond(503, {'detail': outcome['error']})
        self.respond(200, status())


server = ThreadingHTTPServer(('127.0.0.1', 8096), Handler)
server.daemon_threads = True
try:
    saved = Path(os.environ.get('DPM_CAMERA_SETTINGS', '/var/lib/dpm-scanner-lab/settings.json'))
    if saved.exists() and saved.stat().st_size <= 65536:
        settings = json.loads(saved.read_text())
        apply({key: settings.get(key, value) for key, value in DEFAULT_CONTROLS.items()})
    else:
        apply(DEFAULT_CONTROLS)
except (OSError, ValueError, TypeError):
    apply(DEFAULT_CONTROLS)


def on_message(bus, message):
    if message.type == Gst.MessageType.ERROR:
        error, debug = message.parse_error()
        with lock:
            state['error'] = str(error)
        print('Camera pipeline error: '+str(error), flush=True)
        loop.quit()
    elif message.type == Gst.MessageType.EOS:
        loop.quit()


bus = pipeline.get_bus()
bus.add_signal_watch()
bus.connect('message', on_message)
signal.signal(signal.SIGTERM, lambda *args: loop.quit())
signal.signal(signal.SIGINT, lambda *args: loop.quit())
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
        raise SystemExit('Camera pipeline could not start')
    loop.run()
finally:
    server.shutdown()
    server.server_close()
    pipeline.set_state(Gst.State.NULL)
    with lock:
        failed = bool(state['error'])
if failed:
    raise SystemExit(1)
