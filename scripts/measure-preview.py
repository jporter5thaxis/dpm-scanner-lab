"""Measure actual MJPEG delivery over HTTP, independently of camera FPS."""
import argparse
import json
import time
import urllib.request

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8095/api/stream')
parser.add_argument('--seconds', type=float, default=6)
args = parser.parse_args()
marker = b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
frames = total = 0
tail = b''
started = time.monotonic()
with urllib.request.urlopen(args.url, timeout=5) as response:
    while time.monotonic()-started < args.seconds:
        chunk = response.read1(65536)
        if not chunk:
            break
        total += len(chunk)
        combined = tail+chunk
        frames += combined.count(marker)
        tail = combined[-(len(marker)-1):]
elapsed = time.monotonic()-started
print(json.dumps({'frames_received': frames, 'elapsed_seconds': round(elapsed,2),
                  'delivered_fps': round(max(0,frames-1)/elapsed,1),
                  'megabits_per_second': round(total*8/elapsed/1000000,2),
                  'mean_frame_bytes': round(total/frames) if frames else None}, indent=2))
