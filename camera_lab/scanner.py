"""One timed trial using the original DPM scanner's QR + Data Matrix preset."""
import logging
import threading
import time

import cv2
import numpy as np

from backend.config import PRESETS, Settings
from backend.vision import Pipeline


class ScanTrial:
    def __init__(self, reader):
        self.reader = reader
        self.settings = Settings(**PRESETS['QR + Data Matrix'])
        self.pipeline = Pipeline()
        self.started = time.monotonic()
        previous = reader.snapshot()
        self.initial_sequence = previous.sequence if previous else None
        self.result = dict(first_read_ms=None, first_confirmed_ms=None, count=0, latest=None, error=None)
        self.anchor = None
        self.stop_event = threading.Event()
        self.worker = threading.Thread(target=self.run, name='camera-lab-decoder', daemon=True)
        self.worker.start()

    def close(self):
        self.stop_event.set()
        self.worker.join()

    def run(self):
        last = self.initial_sequence
        try:
            while not self.stop_event.is_set():
                tick = time.monotonic()
                frame = self.reader.snapshot()
                if (self.reader.state == 'RUNNING' and frame and frame.sequence != last
                        and frame.captured >= self.started and tick - frame.captured < 2):
                    last = frame.sequence
                    report = self.pipeline.process(frame.image, self.settings, timestamp=frame.captured)
                    elapsed = round((time.monotonic() - self.started) * 1000)
                    result = self.result.copy()
                    if report['reads'] and result['first_read_ms'] is None:
                        result['first_read_ms'] = elapsed
                    for item in report['confirmed']:
                        if result['first_confirmed_ms'] is None:
                            result['first_confirmed_ms'] = elapsed
                        result['count'] += 1
                        result['latest'] = {key: item[key] for key in ('value', 'symbology')}
                    self.result = result
                    tracks = [track for track in report['tracks'] if track['value'] and track['state'] != 'LOST']
                    self.anchor = None
                    if tracks:
                        h, w = frame.image.shape[:2]
                        width = min(960, w)
                        height = max(1, round(h * width / w))
                        gray = cv2.cvtColor(frame.image, cv2.COLOR_BGR2GRAY)
                        if width < w:
                            gray = cv2.resize(gray, (width, height), interpolation=cv2.INTER_AREA)
                        scale = np.array([width / w, height / h])
                        positions = [dict(t, points=(np.asarray(t['points']) * scale).tolist()) for t in tracks]
                        self.anchor = (frame.captured, gray, positions)
                self.stop_event.wait(max(.001, 1 / self.settings.processing_fps - (time.monotonic() - tick)))
        except Exception:
            logging.exception('Camera lab scan failed')
            self.result = dict(self.result, error='Scan interrupted. Start a new scan to retry.')
        finally:
            self.anchor = None
