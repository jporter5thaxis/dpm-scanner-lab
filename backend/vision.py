"""Measured detection, optical-flow tracking, decoding and independent confirmation."""
from collections import deque
from dataclasses import dataclass, field
import re
import time
import cv2
import numpy as np
import zxingcpp
from .config import Settings

cv2.setNumThreads(2)


def quality(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    return {'sharpness': round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2),
            'contrast': round(float(gray.std()), 2),
            'glare_fraction': round(float(np.mean(gray >= 250)), 4),
            'dark_fraction': round(float(np.mean(gray <= 5)), 4)}


def stages(image, cfg: Settings):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    normalized = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX)
    lut = np.array([np.clip(255 * (v / 255) ** cfg.gamma * cfg.contrast, 0, 255) for v in range(256)], np.uint8)
    normalized = cv2.LUT(normalized, lut)
    clahe = cv2.createCLAHE(cfg.clahe_clip, (cfg.clahe_grid, cfg.clahe_grid)).apply(normalized)
    denoised = cv2.GaussianBlur(clahe, (cfg.noise, cfg.noise), 0)
    sharpened = cv2.addWeighted(denoised, 1 + cfg.sharpen, cv2.GaussianBlur(denoised, (0, 0), 2), -cfg.sharpen, 0)
    threshold = cv2.adaptiveThreshold(sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, cfg.threshold_block, cfg.threshold_offset)
    binary = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    if cfg.morphology != 'none':
        op = cv2.MORPH_OPEN if cfg.morphology == 'open' else cv2.MORPH_CLOSE
        binary = cv2.morphologyEx(binary, op, np.ones((2, 2), np.uint8))
    return {'raw': image, 'grayscale': gray, 'normalized': normalized, 'clahe': clahe,
            'filtered': denoised, 'sharpened': sharpened, 'adaptive': threshold,
            'binary': binary, 'inverted': 255 - binary}


def ordered(points):
    p = np.asarray(points, np.float32).reshape(4, 2)
    center = p.mean(axis=0)
    p = p[np.argsort(np.arctan2(p[:, 1] - center[1], p[:, 0] - center[0]))]
    return np.roll(p, -np.argmin(p.sum(axis=1)), axis=0)


def rectify(image, points):
    p = ordered(points)
    size = max(24, min(1200, int(max(np.linalg.norm(p[i] - p[(i + 1) % 4]) for i in range(4)))))
    target = np.float32([[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]])
    return cv2.warpPerspective(image, cv2.getPerspectiveTransform(p, target), (size, size))


def decode_variants(source, cfg, deadline):
    # Try the original image first. Build enhanced images only if that attempt
    # fails and there is still time to use them.
    yield 'raw', source
    if cfg.preprocess and time.perf_counter() < deadline:
        # Build just the next fallback, rather than all nine engineering stages.
        normalized = cv2.normalize(source, None, 0, 255, cv2.NORM_MINMAX)
        lut = np.clip(255 * (np.arange(256) / 255) ** cfg.gamma * cfg.contrast, 0, 255).astype(np.uint8)
        clahe = cv2.createCLAHE(cfg.clahe_clip, (cfg.clahe_grid, cfg.clahe_grid)).apply(cv2.LUT(normalized, lut))
        yield 'clahe', clahe
        if time.perf_counter() >= deadline:
            return
        filtered = cv2.GaussianBlur(clahe, (cfg.noise, cfg.noise), 0)
        sharpened = cv2.addWeighted(filtered, 1 + cfg.sharpen, cv2.GaussianBlur(filtered, (0, 0), 2), -cfg.sharpen, 0)
        yield 'adaptive', cv2.adaptiveThreshold(sharpened, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                               cv2.THRESH_BINARY, cfg.threshold_block, cfg.threshold_offset)


def dpm_input(gray, scale):
    """Bounded full-field DPM pass: suppress fine texture before local thresholding.

    This is an acquisition alternative, not a replacement for native-resolution
    raw decoding. In particular, very small modules still need original pixels.
    """
    image = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    image = cv2.GaussianBlur(image, (0, 0), 1.2)
    return cv2.adaptiveThreshold(image, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                cv2.THRESH_BINARY, 31, 2)


def move_quad(previous, current, points):
    """Track barcode texture, reject inconsistent flow and never invent a decode."""
    if previous is None or previous.shape != current.shape:
        return None
    p = np.asarray(points, np.float32).reshape(4, 2)
    x, y, w, h = cv2.boundingRect(p)
    x, y = max(0, x), max(0, y)
    right, bottom = min(current.shape[1], x+w), min(current.shape[0], y+h)
    if right-x < 6 or bottom-y < 6:
        return None
    roi = previous[y:bottom, x:right]
    features = cv2.goodFeaturesToTrack(roi, maxCorners=24, qualityLevel=.02, minDistance=2)
    if features is None or len(features) < 4:
        return None
    # A small shared window avoids rebuilding 1080p pyramids for a 40 px code.
    # Larger jumps are reacquired by the next native full-frame decode.
    margin = int(max(48, max(w, h)*.8))
    ox, oy = max(0, x-margin), max(0, y-margin)
    ex, ey = min(current.shape[1], right+margin), min(current.shape[0], bottom+margin)
    features += np.float32([x-ox, y-oy])
    previous_window, current_window = previous[oy:ey, ox:ex], current[oy:ey, ox:ex]
    try:
        target, status, _ = cv2.calcOpticalFlowPyrLK(previous_window, current_window, features, None,
                                                   winSize=(21, 21), maxLevel=3)
        if target is None:
            return None
        back, reverse_status, _ = cv2.calcOpticalFlowPyrLK(current_window, previous_window, target, None,
                                                         winSize=(21, 21), maxLevel=3)
        if back is None:
            return None
        good = status.ravel().astype(bool) & reverse_status.ravel().astype(bool)
        good &= np.linalg.norm(back-features, axis=2).ravel() < 1.5
        if good.sum() < 4 or good.mean() < .6:
            return None
        matrix, inliers = cv2.estimateAffinePartial2D(features[good], target[good], method=cv2.RANSAC,
                                                     ransacReprojThreshold=2)
        if matrix is None or inliers.mean() < .6 or not .65 < np.linalg.det(matrix[:, :2]) < 1.5:
            return None
        moved = cv2.transform((p-[ox, oy]).astype(np.float32).reshape(1, 4, 2), matrix)[0]+[ox, oy]
        if not np.isfinite(moved).all() or np.any(moved < 0) or np.any(moved >= [current.shape[1], current.shape[0]]):
            return None
        crop = rectify(current, moved)
        if crop.std() < 3:
            return None
        return moved.astype(np.float32)
    except cv2.error:
        return None


class PreviewTracker:
    """Propagate a decoder's measured positions onto each current preview frame."""
    def __init__(self):
        self.anchor_time = None
        self.previous = None
        self.tracks = []

    def update(self, gray, captured, anchor):
        if anchor is None or not 0 <= captured-anchor[0] < .65:
            self.__init__()
            return []
        measured, source, tracks = anchor
        if self.anchor_time != measured:
            self.anchor_time = measured
            self.previous = source
            self.tracks = [dict(t) for t in tracks if t.get('value') and t['state'] != 'LOST'
                           and measured-t.get('decoded_at', measured) < .65]
        current = []
        for track in self.tracks:
            if captured-track.get('decoded_at', measured) >= .65:
                continue
            points = np.asarray(track['points'], np.float32)
            moved = points if captured == measured else move_quad(self.previous, gray, points)
            if moved is not None:
                current.append({**track, 'points': moved.tolist()})
        self.previous = gray
        self.tracks = current
        return current


@dataclass
class Track:
    id: int
    points: np.ndarray
    first_seen: float
    last_seen: float
    processed_frame: int = 0
    state: str = 'CANDIDATE'
    frames: int = 1
    attempts: int = 0
    reacquisitions: int = 0
    lost_events: int = 0
    motion: float = 0
    value: str | None = None
    decoded_at: float = -1e9
    recovery_scale: float | None = None
    history: deque = field(default_factory=lambda: deque(maxlen=5))
    crops: deque = field(default_factory=lambda: deque(maxlen=3))


class Validator:
    def __init__(self):
        self.evidence = {}
        self.present = {}

    def reset(self):
        self.evidence.clear()
        self.present.clear()

    def accept(self, value, symbology, frame_id, now, cfg):
        if symbology not in ('QR Code', 'Data Matrix') or not value:
            return False, 'Unsupported or empty payload'
        if not re.fullmatch(cfg.payload_pattern, value) or not value.startswith(cfg.prefix):
            return False, 'Payload does not match validation policy'
        if cfg.expected_length and len(value) != cfg.expected_length:
            return False, 'Payload length does not match'
        if cfg.whitelist and value not in cfg.whitelist:
            return False, 'Payload is not on the test whitelist'
        # Absence is defined by elapsed time without a valid observation.
        last_seen, emitted = self.present.get(value, (-1e9, False))
        if now - last_seen > cfg.duplicate_seconds:
            emitted = False
            self.evidence.pop(value, None)
        self.present[value] = (now, emitted)
        if emitted:
            return False, 'Duplicate suppressed'
        frames = self.evidence.setdefault(value, deque(maxlen=cfg.confirmation_frames))
        if not frames or frames[-1] != frame_id:
            frames.append(frame_id)
        if len(frames) < cfg.confirmation_frames:
            return False, 'Awaiting agreement from independent frames'
        self.present[value] = (now, True)
        return True, 'Confirmed'


class Pipeline:
    def __init__(self):
        self.tracks = []
        self.next_id = 1
        self.frame_id = 0
        self.previous_gray = None
        self.validator = Validator()
        self.qr = cv2.QRCodeDetector()
        self.lost_events = 0
        self.missed_frames = 0

    def reset(self):
        self.__init__()

    def discover(self, image, cfg):
        scale = cfg.search_scale
        small = cv2.resize(image, None, fx=scale, fy=scale)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        proposals = []
        try:
            ok, points = self.qr.detectMulti(gray) if cfg.barcode_mode == 'qr_and_data_matrix' else (False, None)
            if ok:
                proposals.extend(p / scale for p in points)
        except cv2.error:
            pass
        # Contours are proposals only: a quadrilateral does NOT imply a barcode.
        edges = cv2.Canny(gray, 60, 160)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:160]:
            p = cv2.approxPolyDP(c, 0.04 * cv2.arcLength(c, True), True)
            if len(p) != 4 or not cv2.isContourConvex(p):
                continue
            x, y, w, h = cv2.boundingRect(p)
            if cfg.min_candidate <= min(w, h) / scale and max(w, h) / scale <= cfg.max_candidate and 0.4 <= w / h <= 2.5:
                crop = gray[y:y+h, x:x+w]
                if crop.std() > 15 and np.mean(edges[y:y+h, x:x+w] > 0) > 0.08:
                    proposals.append(p.reshape(4, 2) / scale)
            if len(proposals) >= 12:
                break
        return proposals

    def associate(self, points, now, decoded=False):
        p = ordered(points)
        center = p.mean(axis=0)
        size = max(20, np.ptp(p, axis=0).max())
        candidates = [(float(np.linalg.norm(t.points.mean(axis=0) - center)), t) for t in self.tracks
                      if decoded or not t.value]
        candidates.sort(key=lambda x: x[0])
        if candidates and candidates[0][0] < size * 0.45:
            t = candidates[0][1]
            if t.state == 'LOST':
                t.reacquisitions += 1
            t.points = p
            t.last_seen = now
            if t.processed_frame != self.frame_id:
                t.frames += 1
                t.processed_frame = self.frame_id
            if t.state == 'LOST':
                t.state = 'TRACKING'
            return t
        if len(self.tracks) >= 12:
            disposable = [t for t in self.tracks if not t.value or t.state == 'LOST']
            if not decoded or not disposable:
                return None
            self.tracks.remove(min(disposable, key=lambda t: t.last_seen))
        t = Track(self.next_id, p, now, now)
        t.processed_frame = self.frame_id
        self.next_id += 1
        self.tracks.append(t)
        return t

    def flow(self, gray, now, cfg):
        if self.previous_gray is None or self.previous_gray.shape != gray.shape:
            self.tracks.clear()
            return
        for t in self.tracks:
            if t.state == 'LOST':
                continue
            moved = move_quad(self.previous_gray, gray, t.points)
            if moved is not None:
                t.motion = round(float(np.linalg.norm(moved-t.points, axis=1).mean()), 2)
                t.points = moved
                t.last_seen = now
                t.frames += 1
                t.processed_frame = self.frame_id
            else:
                t.state = 'LOST'
                t.lost_events += 1
                self.lost_events += 1
        self.tracks = [t for t in self.tracks if now-t.last_seen <= cfg.candidate_timeout
                       and (not t.value or now-t.decoded_at < max(.65, cfg.candidate_timeout))]

    def process(self, image, cfg: Settings, timestamp=None):
        begin = time.perf_counter()
        now = time.monotonic() if timestamp is None else timestamp
        self.frame_id += 1
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        self.flow(gray, now, cfg)
        self.previous_gray = gray
        deadline = begin + cfg.decoder_budget_ms / 1000
        attempts, decode_ms = 0, 0.0
        decoded, confirmed, reads = [], [], []
        values_in_frame = set()
        formats = [zxingcpp.BarcodeFormat.DataMatrix]
        if cfg.barcode_mode == 'qr_and_data_matrix':
            formats.append(zxingcpp.BarcodeFormat.QRCode)

        def read(source, label, origin=(0, 0), scale=1, downscale=False, binary=False):
            nonlocal attempts, decode_ms
            started = time.perf_counter()
            results = zxingcpp.read_barcodes(source, formats=formats, try_rotate=True,
                                            try_downscale=downscale, try_invert=True,
                                            binarizer=zxingcpp.Binarizer.BoolCast if binary else zxingcpp.Binarizer.LocalAverage)
            attempts += 1
            decode_ms += (time.perf_counter()-started)*1000
            valid = False
            for result in results:
                if not result.valid:
                    continue
                valid = True
                if result.text in values_in_frame:
                    continue
                values_in_frame.add(result.text)
                corners = (result.position.top_left, result.position.top_right,
                           result.position.bottom_right, result.position.bottom_left)
                points = np.float32([[v.x/scale+origin[0], v.y/scale+origin[1]] for v in corners])
                t = self.associate(points, now, decoded=True)
                if t:
                    t.value = result.text
                    t.decoded_at = now
                    t.state = 'DECODED'
                    t.attempts += 1
                    if binary:
                        t.recovery_scale = scale
                sym = 'Data Matrix' if result.format == zxingcpp.BarcodeFormat.DataMatrix else 'QR Code'
                accepted, reason = self.validator.accept(result.text, sym, self.frame_id, now, cfg)
                eligible = reason in ('Confirmed', 'Duplicate suppressed', 'Awaiting agreement from independent frames')
                if t and self.validator.present.get(result.text, (0, False))[1]:
                    t.state = 'CONFIRMED'
                q = quality(source if source.size < 160000 else cv2.resize(source, (480, 270)))
                if t:
                    t.history.append(q)
                item = {'value': result.text, 'symbology': sym, 'pipeline': f'ZXing-C++ / {label}',
                        'frame_id': self.frame_id, 'reason': reason, 'track_id': t.id if t else None,
                        'confirmation_ms': round((now-t.first_seen)*1000, 2) if t else None,
                        'attempts': t.attempts if t else attempts, 'frames': t.frames if t else self.frame_id,
                        'width': image.shape[1], 'height': image.shape[0], 'quality': q,
                        'points': points.tolist(), 'eligible': eligible}
                decoded.append(item)
                if eligible:
                    reads.append(item)
                if accepted:
                    confirmed.append(item)
            return valid

        def crop_input(t):
            # Keep real pixels around the symbol, including its quiet zone. Axis-aligned
            # crops retain module detail and the decoder handles arbitrary rotation.
            margin = max(8, float(np.ptp(t.points, axis=0).max())*.3)
            left, top = np.maximum(0, np.floor(t.points.min(axis=0)-margin)).astype(int)
            right, bottom = np.minimum([gray.shape[1], gray.shape[0]], np.ceil(t.points.max(axis=0)+margin)).astype(int)
            return gray[top:bottom, left:right], (int(left), int(top))

        if cfg.auto_decode:
            # A current tracked crop is usually tiny relative to a 1080p frame.
            known = [t for t in self.tracks if t.value and t.state != 'LOST' and now-t.decoded_at < .65]
            for t in known[:3]:
                if attempts and time.perf_counter() >= deadline:
                    break
                source, origin = crop_input(t)
                # Reuse the treatment that acquired this symbol. It avoids
                # repeatedly failing raw crops of a low-contrast engraved mark.
                recovered = False
                if cfg.preprocess and t.recovery_scale is not None:
                    recovered = read(dpm_input(source, t.recovery_scale), f'tracked ROI T{t.id} / DPM',
                                     origin, scale=t.recovery_scale, binary=True)
                if not recovered:
                    read(source, f'tracked ROI T{t.id} / raw', origin)
            # Full-resolution acquisition cannot be starved by contour discovery.
            # Periodic global search also detects a second holder entering the view.
            global_search = not decoded or self.frame_id % cfg.search_interval == 0
            if global_search and cfg.preprocess and cfg.barcode_mode == 'data_matrix' and max(gray.shape) >= 640:
                # Run before an expensive raw scan can consume the whole budget.
                # Coordinates are mapped back to the original capture below.
                scale = min(1, 960/max(gray.shape))
                if read(dpm_input(gray, scale), 'full frame / fast DPM', scale=scale, binary=True):
                    global_search = False
            if global_search:
                # A single native call can exceed the budget on a textured metal
                # scene. Rotate acquisition treatments across missed frames so
                # contrast recovery cannot be starved forever by the raw pass.
                acquisition = self.missed_frames % 6 if cfg.preprocess else 0
                if acquisition in (2, 4):
                    variants = iter(decode_variants(gray, cfg, float('inf')))
                    next(variants)
                    variant, source = next(variants)
                    if acquisition == 4:
                        variant, source = next(variants)
                    read(source, f'full frame / {variant}')
                else:
                    read(gray, 'full frame / multi-scale' if acquisition == 5 else 'full frame / raw',
                         downscale=acquisition == 5)
            if not decoded and time.perf_counter() < deadline:
                for points in self.discover(gray, cfg):
                    self.associate(points, now)
                candidates = [t for t in self.tracks if t.state != 'LOST'][:4]
                inputs = [(f'ROI T{t.id}', *crop_input(t)) for t in candidates]
                inputs.append(('full frame', gray, (0, 0)))
                for label, source, origin in inputs:
                    if time.perf_counter() >= deadline:
                        break
                    if label != 'full frame' and read(source, f'{label} / raw', origin):
                        break
                    # Upscale only small ROIs; full-frame input retains native pixels.
                    if label != 'full frame' and min(source.shape) < 100:
                        enlarged = cv2.resize(source, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
                        if read(enlarged, f'{label} / 2x', origin, scale=2):
                            break
                    for variant, inp in decode_variants(source, cfg, deadline):
                        if time.perf_counter() >= deadline:
                            break
                        if variant == 'raw':
                            continue  # Already tried above, or enhanced proposal is next.
                        if read(inp, f'{label} / {variant}', origin):
                            break
                    if decoded:
                        break
            if not decoded and time.perf_counter() < deadline:
                # Expensive multi-scale search is a last resort, not the common path.
                read(gray, 'full frame / multi-scale', downscale=True)

        self.missed_frames = 0 if decoded else self.missed_frames+1

        overlay = image.copy()
        if cfg.overlays:
            for t in self.tracks:
                if not t.value or t.state == 'LOST' or now-t.decoded_at >= .65:
                    continue
                points = t.points.astype(np.int32)
                cv2.polylines(overlay, [points], True, (70, 225, 65), 2)
                cv2.putText(overlay, 'DATA MATRIX' if cfg.barcode_mode == 'data_matrix' else 'CODE',
                            tuple(points[0]), cv2.FONT_HERSHEY_SIMPLEX, .5, (70, 225, 65), 1)
        diagnostics = [{'id': t.id, 'state': t.state, 'points': t.points.tolist(),
                        'decoded_at': t.decoded_at, 'age_ms': round((now-t.first_seen)*1000),
                        'frames': t.frames, 'attempts': t.attempts, 'motion_px': t.motion,
                        'reacquisitions': t.reacquisitions, 'lost_events': t.lost_events,
                        'quality': t.history[-1] if t.history else None, 'value': t.value} for t in self.tracks]
        active = any(t.value and t.state != 'LOST' and now-t.decoded_at < .65 for t in self.tracks)
        state = 'CONFIRMED' if confirmed else ('DECODED' if decoded else ('TRACKING' if active else 'SEARCHING'))
        frame_quality = quality(cv2.resize(gray, (480, 270)))
        return {'confirmed': confirmed, 'decoded': decoded, 'reads': reads, 'tracks': diagnostics,
                'state': state, 'attempts': attempts, 'decode_ms': round(decode_ms, 2),
                'processing_ms': round((time.perf_counter()-begin)*1000, 2),
                'quality': frame_quality, 'lost_events': self.lost_events, 'overlay': overlay}
