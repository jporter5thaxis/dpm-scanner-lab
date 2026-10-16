# IoTiView scanner integration handoff

The deliverable is a working camera and scanner implementation. A browser UI,
a native camera producer, the capture consumer, preview encoder and barcode
decoder all contribute to the result. Installing camera parameters alone will
not transfer all the improvements.

## Responsibilities

| Layer | Runs on | Change to preserve |
| --- | --- | --- |
| Native camera | Each camera-equipped Jetson | IMX219 Argus processing, bounded exposure and white balance |
| Consumer | Each camera-equipped Jetson | Identify loopback read-only; preserve producer format and buffers |
| Scanner | Station-local service | Native DPM decoding, current-frame crops, independent confirmation |
| Preview | Station-local worker | Newest image, shared encoding, no dependency on decode lock |
| Interface | IoTiView browser | Full preview, settings, read feedback and confirmed results |
| Business actions | IoTiView application | Holder lookup, authorization and idempotent inventory actions |

The headless central web server does not need a camera plugged into it. Each
station owns its camera and can expose controlled results through the app's
authenticated origin or a station gateway. Do not hardcode one station address
as the camera source for all screens.

## Camera consumer correction

`backend/camera.py` queries VIDIOC_QUERYCAP and recognizes v4l2loopback. For
producer-owned virtual cameras it does not call the OpenCV setters for width,
height, fps or buffer count. Those changes previously invalidated the running
producer. Ordinary USB camera configuration retains its own path.

Reuse an already-running virtual camera. Do not start another Argus pipeline
for the same native sensor. Exposure and white balance are physical-camera
properties shared by all consumers of that camera.

## Smooth preview correction

`Runtime._run_preview` takes fresh capture snapshots without acquiring the
decoder lock. It resizes for display (960x540), encodes JPEG at quality 70, and
caps encoding at 24 fps. `/api/stream` checks for the newest shared JPEG every
10 ms. It does not queue delayed images for each viewer.

The capture and decoder source remain 1920x1080. The DPM algorithm may use a
bounded-size acquisition pass, but original pixels remain available for crops
and difficult-code fallback. Show acquisition, preview and processing fps
separately; a 30 fps request is not a measured delivery rate.

## Decoder and tracking correction

`backend/vision.py` uses ZXing-C++ with Data Matrix selected by the working
profile, plus optional QR support. The 10x10-cell symbol is a tuning target,
not an enforced symbol-size filter. Full-frame DPM acquisition smooths texture
and applies adaptive thresholding. Native-resolution fallback is retained.
Tracked crops reuse a successful treatment, and expensive enhancement is lazy.
Missed frames rotate additional treatments to avoid starving contrast recovery.
The decoder budget is checked between native calls; it cannot interrupt one.

Green outlines start from real decoded corners and follow checked optical
flow on fresh preview frames. Invalid motion, lost texture, or 650 ms without
fresh decoded evidence removes the outline. A green box is decoded-code
feedback and may precede policy/confirmation; it is not an inventory event.

There are three results to preserve:

1. **Decoded:** a native decoder found the symbol and its location.
2. **Eligible read:** payload policy accepts the observation for immediate field
   feedback and the repeat-read counter.
3. **Confirmed:** two independent processed frames agree and the duplicate gate
   permits a saved scan. Only this event should feed authorized business actions.

Repeated eligible reads keep the UI responsive without creating repeated saved
scans. The default duplicate policy requires more than three seconds without
a valid observation before re-emission; glare can create such a gap. Add
business-level idempotency rather than treating a tracking outline as a receipt.

## Integration surfaces

| Endpoint | Purpose |
| --- | --- |
| `GET /api/stream` | Shared MJPEG preview; avoid proxy buffering and stale caching |
| `GET /api/status` | Capture/preview/processing metrics, state and latest confirmation |
| `GET /api/scan-events` | SSE with read epoch, cumulative count and latest eligible read |
| `GET /api/scans` | Confirmed lab records; use record identity when consuming results |
| `PATCH /api/settings` | Validated controls and processing settings |
| `GET /api/export/calibration` | Export settings plus requested native controls |
| `POST /api/camera/start` | Start shared application capture consumer |

Settings include source format and native controls. Apply hardware controls
before persisting a claimed change, as the current API does. Import the
`settings` member from a full diagnostic export, or use the settings-only
example in `config/calibration.example.json`.

## Pilot and fleet rollout

Compare IoTiView and this app on the same camera, holder, focus, exposure and
lighting. If IoTiView still misses a mark the reference reads, inspect its
actual decoder input and acquisition strategy before changing drivers again.
Either adapt this pipeline into the existing stack or consume the station
scanner service's preview and confirmed results.

The reference lens was manually focused at about six inches. Set every lens
at its intended working distance; copying JSON cannot transfer physical focus.
Validate readable and difficult marks, motion, glare, reintroduction, duplicate
handling, reconnect and reboot/session behavior.

For many stations, keep ownership, IDs, calibration and events per station.
Twenty display clients are not proof that one global face/scanner worker can
process twenty independent cameras. Benchmark network and inference load,
separating local preview cadence from detection sampling. This repository is
the barcode scanner reference; face models and identity logic are not included.
