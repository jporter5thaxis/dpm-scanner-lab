"""Real 10x10 ECC200 reads, tracking and repeat-read feedback regressions."""
import cv2
import numpy as np
import pytest
import zxingcpp
from backend.config import ConfigStore, Settings
from backend.runtime import Runtime
from backend.storage import Database
from backend.vision import Pipeline, PreviewTracker


from scripts.synthetic_data_matrix import scene, symbol


@pytest.mark.parametrize('rotation', [0, 90, 180, 270, 27])
@pytest.mark.parametrize('condition', ['clean', 'inverted', 'low_contrast', 'blur'])
def test_ten_by_ten_reads_across_rotation_and_surface_conditions(rotation, condition):
    report = Pipeline().process(scene(rotation=rotation, condition=condition), Settings(decoder_budget_ms=120))
    assert [r['value'] for r in report['reads']] == ['123456']
    assert report['reads'][0]['symbology'] == 'Data Matrix'


@pytest.mark.parametrize('module', [2, 3, 5, 8])
def test_small_symbols_acquire_at_full_resolution(module):
    report = Pipeline().process(scene(module=module, x=1100), Settings())
    assert report['reads'][0]['value'] == '123456'
    assert np.asarray(report['reads'][0]['points'])[:, 0].min() > 1100


@pytest.mark.parametrize('module', [8, 16, 32, 48])
def test_larger_ten_by_ten_symbols_remain_readable_during_motion(module):
    # The physical target is 25.4 mm square. Pixel coverage varies with optics
    # and distance; these 80-480 px grids exercise that range without assuming
    # a calibrated conversion from millimetres to camera pixels.
    pipeline = Pipeline()
    for frame in range(3):
        x, y = 900+frame*10, 140+frame*5
        image = scene(x=x, y=y, module=module, width=1920, height=1080)
        report = pipeline.process(image, Settings(), timestamp=frame/24)
        assert [r['value'] for r in report['reads']] == ['123456']
        center = np.asarray(report['reads'][0]['points']).mean(axis=0)
        np.testing.assert_allclose(center, [x+7*module, y+7*module], atol=2)


def test_tilted_symbol_decodes_without_rectification_or_losing_coordinates():
    code = symbol(module=6)
    h, w = code.shape[:2]
    source = np.float32([[0, 0], [w-1, 0], [w-1, h-1], [0, h-1]])
    target = np.float32([[6, 14], [w-7, 0], [w-1, h-10], [0, h-1]])
    warped = cv2.warpPerspective(code, cv2.getPerspectiveTransform(source, target), (w, h),
                                borderValue=(170, 170, 170))
    frame = np.full((720, 1280, 3), 170, np.uint8)
    frame[240:240+h, 710:710+w] = warped
    result = Pipeline().process(frame, Settings())
    assert result['reads'][0]['value'] == '123456'
    points = np.asarray(result['reads'][0]['points'])
    assert (points.min(axis=0) >= [710, 240]).all()
    assert (points.max(axis=0) <= [710+w, 240+h]).all()


def test_motion_repeats_are_reads_but_not_duplicate_saved_scans():
    pipeline = Pipeline()
    reports = [pipeline.process(scene(x=400+i*12, y=160+i*5), Settings(), timestamp=i/24) for i in range(12)]
    assert all([r['value'] for r in report['reads']] == ['123456'] for report in reports)
    assert sum(len(r['confirmed']) for r in reports) == 1
    assert any('tracked ROI' in r['reads'][0]['pipeline'] for r in reports[1:])
    for i, report in enumerate(reports):
        corners = np.asarray(report['reads'][0]['points'])
        np.testing.assert_allclose(corners.mean(axis=0), [400+i*12+28, 160+i*5+28], atol=2)


def test_preview_tracks_the_current_frame_then_hides_missing_or_expired_codes():
    first = scene(x=400, y=200)
    report = Pipeline().process(first, Settings(), timestamp=1)
    gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
    anchor = (1, gray, report['tracks'])
    tracker = PreviewTracker()
    assert tracker.update(gray, 1, anchor)
    for i in range(1, 5):
        current = cv2.cvtColor(scene(x=400+i*6, y=200+i*3), cv2.COLOR_BGR2GRAY)
        tracks = tracker.update(current, 1+i/24, anchor)
        assert len(tracks) == 1
        np.testing.assert_allclose(np.asarray(tracks[0]['points']).mean(axis=0), [428+i*6, 228+i*3], atol=2)
    assert not tracker.update(np.full_like(gray, 170), 1.25, anchor)
    assert not tracker.update(gray, 1.7, anchor)


def test_disabled_decoding_or_rejected_payload_never_counts_as_a_scan():
    pipeline = Pipeline()
    cfg = Settings(prefix='TOOL-')
    report = pipeline.process(scene(), cfg)
    assert report['decoded'] and not report['reads'] and not report['confirmed']
    report = pipeline.process(scene(), cfg.model_copy(update={'auto_decode': False}))
    assert not report['reads'] and not report['confirmed']


def test_data_matrix_mode_excludes_qr_but_mixed_mode_remains_available():
    raw = np.asarray(zxingcpp.create_barcode('QR-TEST', zxingcpp.BarcodeFormat.QRCode).to_image(scale=5))
    frame = cv2.cvtColor(raw, cv2.COLOR_GRAY2BGR)
    assert not Pipeline().process(frame, Settings())['reads']
    assert Pipeline().process(frame, Settings(barcode_mode='qr_and_data_matrix'))['reads'][0]['value'] == 'QR-TEST'


def test_feedback_keeps_repeat_reads_and_resets_without_inserting_scans(tmp_path):
    runtime = Runtime(ConfigStore(tmp_path), Database(tmp_path/'reads.sqlite3'), tmp_path)
    item = {'value': '123456', 'symbology': 'Data Matrix', 'frame_id': 1, 'pipeline': 'test'}
    runtime.on_read(item)
    runtime.on_read({**item, 'frame_id': 2})
    assert runtime.read_feedback['read_count'] == 2
    assert runtime.read_feedback['latest_read']['value'] == '123456'
    assert not runtime.db.rows('scans')
    epoch = runtime.read_feedback['read_epoch']
    runtime.reset_reads(reset_count=True)
    assert runtime.read_feedback['read_count'] == 0 and runtime.read_feedback['latest_read'] is None
    assert runtime.read_feedback['read_epoch'] != epoch


def test_background_contour_never_moves_a_decoded_track():
    pipeline = Pipeline()
    report = pipeline.process(scene(), Settings())
    code = next(t for t in pipeline.tracks if t.value)
    original = code.points.copy()
    pipeline.associate(original+4, code.last_seen+.01)
    np.testing.assert_array_equal(code.points, original)


def test_slow_raw_calls_cannot_starve_contrast_recovery(monkeypatch):
    import time
    import backend.vision as module
    original = module.zxingcpp.read_barcodes
    calls = []
    def slow_decode(image, **kwargs):
        calls.append(image)
        time.sleep(.012)
        return [] if len(calls) < 3 else original(image, **kwargs)
    monkeypatch.setattr(module.zxingcpp, 'read_barcodes', slow_decode)
    pipeline = Pipeline()
    cfg = Settings(decoder_budget_ms=10)
    reports = [pipeline.process(scene(), cfg) for _ in range(3)]
    recovered = [r for r in reports if r['reads']]
    assert recovered and recovered[0]['reads'][0]['value'] == '123456'
    assert any(treatment in recovered[0]['reads'][0]['pipeline'] for treatment in ('DPM', 'clahe'))
