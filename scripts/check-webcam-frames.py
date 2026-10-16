"""Bounded validation of the scanner's /dev/video10 webcam feed."""
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parents[1]
if '--child' not in sys.argv:
    try:
        result = subprocess.run([sys.executable,__file__,'--child'],capture_output=True,text=True,timeout=15)
        report = json.loads(result.stdout) if result.stdout.strip() else {'frames_verified':False,'error':result.stderr[-1500:]}
    except subprocess.TimeoutExpired:
        report = {'frames_verified':False,'error':'Virtual camera did not return frames within 15 seconds'}
    (root/'data/webcam-validation.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report['frames_verified'] else 2)

import cv2
capture = cv2.VideoCapture(10,cv2.CAP_V4L2)
try:
    if not capture.isOpened():
        print(json.dumps({'frames_verified':False,'error':'/dev/video10 does not currently expose a readable camera feed'}))
        raise SystemExit(2)
    frames = []
    started = time.monotonic()
    image = None
    for _ in range(10):
        ok, image = capture.read()
        if not ok or image is None:
            break
        frames.append(time.monotonic())
    ok = len(frames) == 10
    report = dict(frames_verified=ok, frames_read=len(frames),elapsed_seconds=round(time.monotonic()-started,3))
    if image is not None:
        report.update(width=image.shape[1],height=image.shape[0],pixel_stddev=round(float(image.std()),2))
        if ok:
            cv2.imwrite(str(root/'data/jetson-camera-first-frame.jpg'),image)
    print(json.dumps(report))
finally:
    capture.release()
