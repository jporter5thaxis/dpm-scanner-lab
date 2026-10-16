"""Bounded offline replay and matched-footage comparison. No live scan events."""
import base64
import threading
import time
import uuid
import cv2
import numpy as np
from .vision import Pipeline, stages, rectify


def image_url(image):
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        raise ValueError('Could not encode image')
    return 'data:image/jpeg;base64,' + base64.b64encode(encoded).decode()


def load_frame(path, index=0):
    if path.suffix.lower() in ('.jpg','.jpeg','.png','.bmp'):
        image = cv2.imread(str(path))
        total, fps = 1, 1
    else:
        cap = cv2.VideoCapture(str(path))
        try:
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, image = cap.read()
            if not ok:
                image = None
        finally:
            cap.release()
    if image is None:
        raise ValueError('The file has no readable image at this frame')
    if image.shape[0]*image.shape[1] > 12_600_000:
        raise ValueError('Images above 12.6 megapixels are not supported in this lab')
    return image, total, fps


def analyze(path, cfg, index=0):
    image, total, fps = load_frame(path,index)
    pipeline = Pipeline()
    result = pipeline.process(image, cfg)
    rendered = stages(image, cfg)
    rendered['candidates'] = result['overlay']
    if pipeline.tracks:
        cropped = rectify(image, pipeline.tracks[0].points)
        rendered['perspective'] = cropped
        rendered['decoder_input'] = stages(cropped, cfg)['clahe']
    else:
        rendered['perspective'] = None
        rendered['decoder_input'] = None
    return {'stages': {k:image_url(v) if v is not None else None for k,v in rendered.items()},
            'frame_count': total, 'fps': fps, 'frame': index, **{k:v for k,v in result.items() if k!='overlay'}}


class ReplayJobs:
    def __init__(self):
        self.jobs = {}
        self.gate = threading.Lock()

    def start(self, paths, configurations, expected, metadata):
        if not self.gate.acquire(blocking=False):
            raise RuntimeError('An offline batch is already running')
        key = uuid.uuid4().hex
        # Retain only the most recent ten jobs, keeping reports bounded.
        if len(self.jobs) >= 10:
            self.jobs.pop(next(iter(self.jobs)))
        self.jobs[key] = {'id': key, 'state': 'running', 'progress': 0, 'runs': [], 'expected': expected, 'metadata': metadata}
        threading.Thread(target=self._run, args=(key, paths, configurations, expected), daemon=True).start()
        return key

    def _run(self, key, paths, configurations, expected):
        job = self.jobs[key]
        try:
            work_total = len(paths)*len(configurations)
            done = 0
            for name, cfg in configurations:
                reports = []
                for path in paths:
                    pipeline = Pipeline()
                    first, total, fps = load_frame(path)
                    cap = None if total == 1 else cv2.VideoCapture(str(path))
                    start = time.perf_counter()
                    frames = 0
                    attempts = 0
                    correct_frames = 0
                    false_frames = 0
                    detected_frames = 0
                    decode_frames = 0
                    first_correct_ms = None
                    processing_times = []
                    values = set()
                    try:
                        for index in range(min(max(total,1),300)):
                            ok, image = (True, first) if cap is None else cap.read()
                            if not ok:
                                break
                            report = pipeline.process(image, cfg, timestamp=index/fps)
                            frames += 1
                            attempts += report['attempts']
                            processing_times.append(report['processing_ms'])
                            detected_frames += bool(report['tracks'])
                            read = {item['value'] for item in report['decoded']}
                            values.update(read)
                            decode_frames += bool(read)
                            if expected:
                                correct_frames += expected in read
                                false_frames += bool(read - {expected})
                                if expected in read and first_correct_ms is None:
                                    first_correct_ms = index/fps*1000
                            if time.perf_counter()-start > 90:
                                break
                    finally:
                        if cap:
                            cap.release()
                    elapsed = time.perf_counter()-start
                    reports.append({'file': path.name, 'frames': frames, 'source_frames': total, 'truncated': frames<total,
                                    'values': sorted(values), 'decode_frames': decode_frames, 'correct_frames': correct_frames if expected else None,
                                    'false_frames': false_frames if expected else None, 'first_correct_source_ms': first_correct_ms,
                                    'detected_frames': detected_frames, 'attempts': attempts, 'processing_fps': round(frames/elapsed,2),
                                    'mean_processing_ms': round(float(np.mean(processing_times)),2) if processing_times else None,
                                    'lost_events': pipeline.lost_events})
                    done += 1
                    job['progress'] = round(100*done/work_total)
                frames = sum(r['frames'] for r in reports)
                job['runs'].append({'preset': name, 'settings': cfg.model_dump(), 'files': reports,
                                    'frame_decode_rate': sum(r['decode_frames'] for r in reports)/frames if frames else 0,
                                    'correct_frame_rate': sum(r['correct_frames'] for r in reports)/frames if frames and expected else None,
                                    'false_frame_rate': sum(r['false_frames'] for r in reports)/frames if frames and expected else None})
            job['state'] = 'complete'
        except Exception as exc:
            job['state'] = 'failed'
            job['error'] = str(exc)
        finally:
            self.gate.release()
