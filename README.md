# DPM Scanner Lab

Working reference implementation for IMX219 camera capture, smooth live preview,
engraved Data Matrix scanning, and decoded-code tracking on NVIDIA Jetson.
The React web interface runs in a browser; capture, enhancement and decoding run
in the Python service on the camera-equipped station.

**IoTiView team: start with [the integration handoff](docs/IOTIVIEW-INTEGRATION.md).**
Applying camera settings alone does not reproduce the decoder and preview fixes.

## What works

- One native IMX219 producer publishes a 1920x1080 virtual webcam.
- The capture consumer identifies v4l2loopback and preserves its producer-owned
  resolution, frame rate and buffers instead of renegotiating them.
- A separate latest-frame preview worker delivers up to 24 fps at 960x540,
  JPEG quality 70, independently of expensive barcode processing.
- Native ZXing-C++ decodes Data Matrix, with optional QR support. Full-field DPM
  acquisition and tracked crops recover contrast while retaining native-resolution
  fallback. Enhancement is generated only when needed.
- Green outlines follow real decoded symbols on fresh preview frames. Optical-flow
  predictions cannot create reads or confirmed scans.
- Eligible reads update the field immediately; saved scans require independent-frame
  agreement and apply an absence-based duplicate policy.
- A large camera view stays visible beside independently scrolling live sliders.
- The engineering view supports captured images/video, stage inspection, presets,
  repeatable trials, and offline comparison.

The reference camera delivered about 26-27 fps and the HTTP preview measured
23-24 fps. Those are measured reference checks, not a guarantee for every scene
or fleet-wide scan accuracy. See [validation scope](docs/VALIDATION.md).

## Working calibration

The reusable baseline is in [config/calibration.example.json](config/calibration.example.json):

- Camera: IMX219, producer sensor ID 0, sensor mode 2, flip method 6.
- Consumer: V4L2 device 10, 1920x1080, requested 30 fps.
- Exposure: automatic, compensation +1.25 EV, white balance Auto.
- Stored manual exposure/gain: 10 ms and 1x; these are not measured auto values.
- Physical focus: manually adjusted at approximately 6 inches / 152.4 mm.
- Code mode: Data Matrix; two independent frames for confirmation.

Focus must be set by turning each lens. The distance field records the setup;
it cannot move a manual lens. Sensor IDs, modes, orientation and device nodes
must be verified for the destination station.

## Local development

Python 3.12 and a supported Node.js version (22.12+ for this Vite version) are required.
From a fresh checkout:

```sh
python -m venv .venv
# Linux/macOS:
.venv/bin/python -m pip install -r requirements-dev.txt
# Windows PowerShell instead:
# .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

Install the frontend using the checked-in lock file:

```sh
corepack enable
pnpm install --frozen-lockfile
pnpm build
```

Start the application, using the appropriate Python path for your system:

```sh
.venv/bin/python scripts/run.py --host 127.0.0.1 --port 8095
```

On Windows, `start-local.ps1` runs the local backend after setup. Open
`http://127.0.0.1:8095`. The camera does not start automatically. For a browser
webcam use localhost or HTTPS; LAN HTTP clients can view the station's server-side
camera stream without browser camera access.

`requirements-jetson-lock.txt` records the exact working ARM64 runtime versions.
It is a reference lock for matching Jetson/Python environments, not a replacement
for release-matched NVIDIA system packages.

## Jetson installation

Follow [the station setup guide](docs/JETSON-SETUP.md). It covers prerequisites,
the native producer, bounded live controls, application service and calibration.
This repository does not flash a board or choose its boot overlay automatically.

The verified reference used Ubuntu 24.04.4, NVIDIA L4T R39.2.0 and kernel
6.8.12-1021-tegra. R39 is the NVIDIA release number, not an Ubuntu version.
Do not install older R36/Ubuntu 22.04 camera packages onto R39.

The public producer changes only the original deployment-specific settings path:
set `DPM_CAMERA_SETTINGS` to the scanner's private `data/settings.json`. Its
capture and camera-control logic otherwise follow the working reference.

## Source map

| Source | Responsibility |
| --- | --- |
| `backend/camera.py` | Capture adapters, read-only loopback detection, latest frame |
| `scripts/jetson-calibration-camera.py` | Native producer and local camera controls |
| `backend/camera_controls.py` | Bounded controls and exposure unit conversion |
| `backend/runtime.py` | Independent decode/preview workers, events and telemetry |
| `backend/vision.py` | DPM acquisition, tracking, validation and duplicate policy |
| `backend/app.py` | MJPEG/SSE, calibration and settings API, lab workflows |
| `frontend/` | Operator UI, live sliders and full camera view |
| `config/` | Reusable calibration examples without station identity |

## Verification

```sh
.venv/bin/python -m pytest tests -q --basetemp .pytest_tmp
.venv/bin/python scripts/benchmark-data-matrix.py
pnpm build
```

Tests use synthetic symbols and isolated data. Operator-supplied photographs,
scan records, private reports, credentials, environments and built distributions
are deliberately excluded from this public source export.

## Access and lifecycle

The lab supports password-free LAN use when `DPM_TOKEN` is empty; do not expose
that deployment directly to an untrusted network. IoTiView integration should
retain its authentication and holder-authorization boundary. The camera-control
listener remains on `127.0.0.1:8096`.

All viewers share the same scanner consumer. Closing the scan dialog stops that
consumer. The user service starts with the login session unless an administrator
chooses a different lifecycle. These behaviors need deliberate adaptation for
multi-user production deployments.

The two small helpers under `deploy/camera-r39/` are retained for historical
configuration-validation tests; they are not an instruction to update a working
station to R39.2.1.
