# Jetson station setup

This guide reproduces the working camera/decoder boundary on a compatible
IMX219 station. It does not flash the board, select an overlay or promise
compatibility with an arbitrary NVIDIA release.

## Preflight

Record the board model, `/etc/nv_tegra_release`, `uname -r`, camera sensor and
port, native modes, current camera owners and intended lens distance. The
reference was Ubuntu 24.04.4 / NVIDIA R39.2.0 / kernel 6.8.12-1021-tegra.

Verify NVIDIA Argus, `nvarguscamerasrc`, `nvvidconv`, system Python PyGObject
and GStreamer bindings, and a kernel-matched v4l2loopback module. Keep NVIDIA
packages, overlays and kernel modules matched to the destination release.
An enumerated sensor or active service is not proof of readable image frames.

If a producer already works, preserve it and test its virtual feed before
replacing it. Stop competing native capture owners for isolated diagnostics.
Never touch the CSI ribbon while powered.

## Application environment

Clone into the deployment account's `~/dpm-scanner-lab`. Install Node/pnpm on
a build machine, or on the Jetson if building there. Build the frontend with
the lock file; Node is not required to serve a prebuilt `dist` directory.

```sh
cd ~/dpm-scanner-lab
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-jetson-lock.txt
corepack enable
pnpm install --frozen-lockfile
pnpm build
mkdir -p data/tmp
```

Ubuntu may need its matching `python3-venv` package first. The private Python
environment contains headless OpenCV for processing and V4L2 consumption;
the separate system producer uses NVIDIA GStreamer. Do not replace a system
OpenCV installation or install R36 ZIP packages on R39 to follow this recipe.

## Native producer and live controls

The reference pipeline uses sensor ID 0, mode 2, flip method 6, and 1920x1080
at a requested 30 fps. Conversion is NVMM NV12 -> I420 -> YUY2, followed by
`identity drop-allocation=true` and `v4l2sink ... sync=false` on `/dev/video10`.
Adapt those values to the actual camera before deployment.

`scripts/jetson-calibration-camera.py` keeps the native producer open and
accepts only a bounded complete control set on `127.0.0.1:8096`. Install its
helper and validator in a root-owned directory, after backing up the station's
existing service and helpers:

```sh
sudo install -d -m 755 /usr/local/lib/dpm-scanner-lab
sudo install -m 644 scripts/jetson-calibration-camera.py \
  /usr/local/lib/dpm-scanner-lab/jetson-calibration-camera.py
sudo install -m 644 backend/camera_controls.py \
  /usr/local/lib/dpm-scanner-lab/camera_controls.py
```

Review `deploy/csi-webcam.service`. Replace `SCANNER_USER` with the destination
account in `DPM_CAMERA_SETTINGS`, and verify `/dev/video10` is the intended
IMX219 virtual camera, owned by this producer. The producer uses the saved
settings file to restore exposure on startup. Do not install a second producer
over a camera used by another application without coordinating its ownership.

```sh
# After adapting and backing up this station's existing unit:
sudo install -m 644 deploy/csi-webcam.service /etc/systemd/system/csi-webcam.service
sudo systemctl daemon-reload
sudo systemctl enable --now csi-webcam.service
curl --fail http://127.0.0.1:8096/status
.venv/bin/python scripts/check-webcam-frames.py
```

The module must already be configured to expose the intended node. The service
only loads `v4l2loopback`; it does not change a loaded module's device layout.
Verify actual fresh frames and no producer restart loop. Do not restart Argus
or reload a busy module merely because discovery succeeded.

## Scanner service and calibration

The user service uses `%h/dpm-scanner-lab` and listens on port 8095. Choose a
different port in its unit if needed; it refuses to displace another listener.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/dpm-scanner-lab-user.service ~/.config/systemd/user/dpm-scanner-lab.service
# Fresh password-free LAN lab only; preserve an existing environment on update.
printf 'DPM_TOKEN=\n' > data/service.env
chmod 600 data/service.env
systemctl --user daemon-reload
systemctl --user enable --now dpm-scanner-lab.service
curl --fail -X PATCH -H 'Content-Type: application/json' \
  --data-binary @config/calibration.example.json http://127.0.0.1:8095/api/settings
curl --fail -X POST http://127.0.0.1:8095/api/camera/start
```

Stop the consumer before applying differing capture properties. Format/fps
changes are restricted on the fixed live-controls feed. Native exposure bounds
are requested camera properties, not measured exposure telemetry. In automatic
mode the saved 10 ms/1x values are inactive manual settings.

Focus the physical lens on the marking at the intended distance, approximately
six inches in the reference setup. Verify the source, preview and decoded mark.

The service starts with the account's login session unless an administrator
deliberately configures linger or a system service. Capture does not auto-start
merely because the scanner service is active. Closing the dialog stops the
shared consumer. Choose production lifecycle behavior explicitly.

The scanner applies Linux Landlock write restrictions and permits the camera
and GPU character devices its libraries need. Its limit is 1500 MB memory and
two CPU cores. Keep the application's ordinary writes inside its private data
directory; do not solve an EGL/device permission failure by disabling all
restrictions.

## Validation and rollback

```sh
systemctl status csi-webcam.service
systemctl --user status dpm-scanner-lab.service
v4l2-ctl -d /dev/video10 --all
curl --fail http://127.0.0.1:8095/api/status
.venv/bin/python scripts/measure-preview.py \
  --url http://127.0.0.1:8095/api/stream --seconds 8
```

Check real frames, preview freshness, readable marks, duplicates, disconnect
and recovery under normal workload. Before updating, back up that station's
application, data/settings, presets, service units and root helpers. Roll back
those matching files and verify frames before resuming capture. Never copy
another station's IP, host keys, `/boot` tree or kernel modules as a shortcut.
