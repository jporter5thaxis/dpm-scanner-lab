"""Single processing worker independent of viewers, with bounded recording/telemetry."""
from collections import deque
import logging
from pathlib import Path
import threading
import time
import uuid
import cv2
import numpy as np
import psutil
from .camera import CameraManager
from .storage import utcnow
from .vision import Pipeline, PreviewTracker

log = logging.getLogger('dpm')


class Runtime:
    def __init__(self, config, db, media):
        self.config, self.db, self.media = config, db, media
        self.camera = CameraManager()
        self.pipeline = Pipeline()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.worker = None
        self.preview_worker = None
        self.preview_tracks = None
        self.preview_fps = 0
        self.preview_captured = 0
        self.preview_resolution = None
        self.paused = False
        self.jpeg = None
        self.raw = None
        self.report = None
        self.session_id = None
        self.trial = None
        self.scan_count = 0
        self.failed_attempts = 0
        self.drops = 0
        self.processing_fps = 0
        self.attempts_per_second = 0
        self.latest_scan = None
        self.read_feedback = {'read_epoch': uuid.uuid4().hex, 'read_count': 0, 'latest_read': None}
        self.recording = None
        self.record_until = 0
        self.record_name = None
        self.telemetry = {}
        self.resource_pressure = False
        self.process = psutil.Process()
        self.cpu_ticks = deque(maxlen=100)

    def start_worker(self):
        self.stop_event.clear()
        self.worker = threading.Thread(target=self._run, name='vision-worker', daemon=True)
        self.worker.start()
        self.preview_worker = threading.Thread(target=self._run_preview, name='preview-worker', daemon=True)
        self.preview_worker.start()

    def _run_preview(self):
        """Encode latest camera frames without waiting for decoding or its lock."""
        last_sequence = -1
        ticks = deque(maxlen=60)
        tracker = PreviewTracker()
        while not self.stop_event.is_set():
            started = time.monotonic()
            frame = self.camera.snapshot()
            if self.camera.state != 'RUNNING' or frame is None or started-frame.captured > 2:
                self.jpeg = None
                self.preview_fps = 0
                ticks.clear()
                tracker = PreviewTracker()
            elif frame.sequence != last_sequence:
                try:
                    scale = min(1, 960/frame.image.shape[1])
                    image = (cv2.resize(frame.image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                             if scale < 1 else frame.image.copy())
                    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
                    if self.config.settings.overlays and not self.paused:
                        tracks = tracker.update(gray, frame.captured, self.preview_tracks)
                        for track in tracks:
                            points = np.asarray(track['points'], np.float32)
                            if points.shape != (4,2):
                                continue
                            points = points.astype(np.int32)
                            color = (70, 225, 65)
                            cv2.polylines(image, [points], True, color, 2)
                            cv2.putText(image, 'DATA MATRIX' if self.config.settings.barcode_mode == 'data_matrix' else 'CODE', tuple(points[0]),
                                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
                    else:
                        tracker = PreviewTracker()
                    ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    if ok and not self.camera.stop_event.is_set() and self.camera.state == 'RUNNING':
                        last_sequence = frame.sequence
                        now = time.monotonic()
                        ticks.append(now)
                        while ticks and now-ticks[0] > 2:
                            ticks.popleft()
                        self.preview_fps = round((len(ticks)-1)/(ticks[-1]-ticks[0]), 1) if len(ticks)>1 else 0
                        self.preview_resolution = [image.shape[1], image.shape[0]]
                        self.preview_captured = frame.captured
                        self.jpeg = jpeg.tobytes()
                except Exception:
                    log.exception('preview_error')
            self.stop_event.wait(max(0.001, 1/24-(time.monotonic()-started)))

    def _run(self):
        last_seq = -1
        processed = deque(maxlen=100)
        attempts = deque(maxlen=100)
        last_telemetry = 0
        while not self.stop_event.is_set():
            cfg = self.config.settings
            self.resource_pressure = self.telemetry.get('available_ram_mb', 10000) < 512 or self.telemetry.get('system_cpu_percent', 0) > 90
            if self.resource_pressure:
                cfg = cfg.model_copy(update={'processing_fps':min(4,cfg.processing_fps), 'decoder_budget_ms':min(40,cfg.decoder_budget_ms), 'search_interval':max(5,cfg.search_interval)})
            loop_start = time.monotonic()
            frame = self.camera.snapshot()
            try:
                with self.lock:
                    if frame and frame.sequence != last_seq:
                        if last_seq >= 0:
                            self.drops += max(0, frame.sequence-last_seq-1)
                        last_seq = frame.sequence
                        self.raw = frame.image.copy()
                        if self.paused:
                            self.preview_tracks = None
                        else:
                            self.report = self.pipeline.process(frame.image, cfg, timestamp=frame.captured)
                            scale = min(1, 960/frame.image.shape[1])
                            anchor_gray = cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY)
                            if scale < 1:
                                anchor_gray = cv2.resize(anchor_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                            tracks = [{**t, 'points': (np.asarray(t['points'])*scale).tolist()}
                                      for t in self.report['tracks'] if t['value'] and t['state'] != 'LOST']
                            self.preview_tracks = (frame.captured, anchor_gray, tracks)
                            for item in self.report['reads']:
                                self.on_read(item)
                            self.failed_attempts += self.report['attempts'] if not self.report['decoded'] else 0
                            for item in self.report['confirmed']:
                                self.on_scan(item)
                            processed.append(loop_start)
                            attempts.append((loop_start, self.report['attempts']))
                        if self.recording:
                            if loop_start >= self.record_until:
                                self._stop_recording()
                            else:
                                self.storage_guard(self.raw.nbytes)
                                self.recording.write(frame.image)
                    if self.recording and loop_start >= self.record_until:
                        self._stop_recording()
                    if self.trial and loop_start >= self.trial['deadline']:
                        self.finish_trial('timeout', None)
                    while processed and loop_start-processed[0] > 2:
                        processed.popleft()
                    self.processing_fps = round((len(processed)-1)/(processed[-1]-processed[0]), 1) if len(processed)>1 else 0
                    self.attempts_per_second = round(sum(n for t,n in attempts if loop_start-t < 2)/2, 1)
                    if loop_start-last_telemetry >= 2:
                        self.telemetry = self.measure()
                        self.db.add('samples', {'processing_fps': self.processing_fps, **self.telemetry})
                        last_telemetry = loop_start
            except Exception:
                log.exception('processing_error')
                self.camera.error = 'Image processing failed; see the independent scanner log'
                with self.lock:
                    if self.recording:
                        self._stop_recording()
            self.stop_event.wait(max(0.001, 1/cfg.processing_fps-(time.monotonic()-loop_start)))

    def measure(self):
        temperature = None
        for p in Path('/sys/class/thermal').glob('thermal_zone*/temp'):
            try:
                temperature = max(temperature or 0, float(p.read_text())/1000)
            except (ValueError, OSError):
                pass
        gpu = None
        for p in (Path('/sys/devices/platform/17000000.gpu/load'), Path('/sys/devices/gpu.0/load')):
            try:
                gpu = float(p.read_text())/10
                break
            except (ValueError, OSError):
                pass
        return {'process_cpu_percent': self.process.cpu_percent(), 'system_cpu_percent': psutil.cpu_percent(),
                'ram_mb': round(self.process.memory_info().rss/1048576, 1), 'available_ram_mb': round(psutil.virtual_memory().available/1048576),
                'gpu_percent': gpu, 'temperature_c': temperature, 'exposure': None}

    def on_scan(self, item):
        item = {**item, 'timestamp': utcnow()}
        if self.config.settings.save_diagnostics:
            try:
                item['diagnostic'] = self.capture()
            except RuntimeError:
                log.warning('diagnostic_storage_full')
        item['id'] = self.db.add('scans', item, self.session_id)
        self.latest_scan = item
        self.scan_count += 1
        if self.trial:
            self.finish_trial('correct' if item['value'] == self.trial['expected'] else 'incorrect', item['value'])

    def on_read(self, item):
        """Count every eligible frame read independently of saved-scan suppression."""
        count = self.read_feedback['read_count'] + 1
        latest = {key: item[key] for key in ('value', 'symbology', 'frame_id', 'pipeline')}
        self.read_feedback = {**self.read_feedback, 'read_count': count,
                              'latest_read': {**latest, 'id': count, 'timestamp': utcnow()}}

    def reset_reads(self, reset_count=False):
        self.read_feedback = ({'read_epoch': uuid.uuid4().hex, 'read_count': 0, 'latest_read': None}
                              if reset_count else {**self.read_feedback, 'latest_read': None})
        self.preview_tracks = None

    def begin_trial(self, data):
        with self.lock:
            if self.trial:
                raise RuntimeError('Finish the active trial first')
            self.pipeline.validator.reset()
            self.trial = {**data, 'started': time.monotonic(), 'deadline': time.monotonic()+data.get('timeout_seconds', 10),
                          'start_attempts': self.failed_attempts, 'frame_start': self.pipeline.frame_id,
                          'settings': self.config.settings.model_dump()}

    def finish_trial(self, outcome, actual):
        with self.lock:
            if not self.trial:
                raise RuntimeError('No active trial')
            trial = self.trial
            result = {k:v for k,v in trial.items() if k not in ('started','deadline','start_attempts','frame_start')}
            result.update({'outcome': outcome, 'actual': actual, 'latency_ms': round((time.monotonic()-trial['started'])*1000,2),
                           'frames': self.pipeline.frame_id-trial['frame_start'], 'failed_decode_calls': self.failed_attempts-trial['start_attempts']})
            self.db.add('trials', result, self.session_id)
            self.trial = None
            return result

    def storage_guard(self, incoming=0):
        used = sum(p.stat().st_size for p in self.media.iterdir() if p.is_file())
        if used + incoming > 512 * 1024 * 1024:
            raise RuntimeError('The 512 MB scanner media limit has been reached. Download and remove older lab files.')

    def capture(self):
        with self.lock:
            frame = self.camera.snapshot()
            image = frame.image if frame and time.monotonic()-frame.captured < 2 else self.raw
            if image is None:
                raise RuntimeError('No camera frame is available')
            ok, img = cv2.imencode('.jpg', image)
            if not ok:
                raise RuntimeError('Frame could not be encoded')
            self.storage_guard(img.nbytes)
            name = f'{uuid.uuid4().hex}.jpg'
            (self.media/name).write_bytes(img.tobytes())
            return name

    def record(self):
        with self.lock:
            if self.raw is None or self.recording:
                raise RuntimeError('Start the camera first, and wait for any active recording to finish')
            self.storage_guard(64*1024*1024)
            self.record_name = f'{uuid.uuid4().hex}.avi'
            h,w = self.raw.shape[:2]
            self.recording = cv2.VideoWriter(str(self.media/self.record_name), cv2.VideoWriter_fourcc(*'MJPG'), self.config.settings.processing_fps, (w,h))
            if not self.recording.isOpened():
                self.recording.release()
                self.recording = None
                raise RuntimeError('MJPEG video recording is unavailable')
            self.record_until = time.monotonic()+5
            return self.record_name

    def _stop_recording(self):
        self.recording.release()
        self.recording = None
        log.info('recording_saved %s', self.record_name)

    def status(self):
        with self.lock:
            report = {k:v for k,v in (self.report or {}).items() if k!='overlay'}
            return {'camera': self.camera.status(), 'paused': self.paused, 'processing_fps': self.processing_fps,
                    'preview': {'fps': self.preview_fps if time.monotonic()-self.preview_captured < 2 else 0,
                                'resolution': self.preview_resolution,
                                'age_ms': round((time.monotonic()-self.preview_captured)*1000) if self.preview_captured else None},
                    'attempts_per_second': self.attempts_per_second, 'drops': self.drops, 'scan_count': self.scan_count,
                    'failed_decode_calls': self.failed_attempts, 'latest_scan': self.latest_scan, 'session_id': self.session_id,
                    'trial_active': bool(self.trial), 'recording': bool(self.recording), 'record_name': self.record_name,
                    'telemetry': self.telemetry, 'resource_pressure': self.resource_pressure,
                    **self.read_feedback, **report}

    def close(self):
        self.camera.stop()
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=5)
        if self.preview_worker:
            self.preview_worker.join(timeout=5)
        with self.lock:
            if self.recording:
                self._stop_recording()
