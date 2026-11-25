import time
from types import SimpleNamespace

import pytest
import numpy as np
import zxingcpp
from fastapi.testclient import TestClient

from camera_lab import app as lab
from camera_lab.history import DetectionHistory
from backend.config import Settings
from backend.vision import Pipeline
from tests.test_vision import barcode


@pytest.mark.parametrize('symbol', [zxingcpp.BarcodeFormat.QRCode, zxingcpp.BarcodeFormat.DataMatrix])
def test_timed_camera_scan(monkeypatch, tmp_path, symbol):
    image = barcode('HOLDER-42', symbol)
    frame = SimpleNamespace(sequence=1, captured=time.monotonic(), image=image)
    monkeypatch.setattr(lab, 'session', 'test')
    monkeypatch.setattr(lab, 'history', DetectionHistory(tmp_path / 'history.db'))
    monkeypatch.setattr(lab.reader, 'state', 'RUNNING')
    monkeypatch.setattr(lab.reader, 'snapshot', lambda: frame)
    with TestClient(lab.app) as client:
        def toggle(enabled):
            return client.post('/api/scan', json={'session': 'test', 'enabled': enabled}).json()['scan']

        def until(key):
            for _ in range(100):
                result = client.get('/api/status').json()['scan']
                if result[key] is not None:
                    return result
                time.sleep(.02)
            pytest.fail(f'No {key} for {symbol}')

        assert toggle(True)['first_read_ms'] is None
        frame = SimpleNamespace(sequence=2, captured=time.monotonic(), image=image)
        assert until('first_read_ms')['count'] == 0
        time.sleep(.1)
        assert client.get('/api/status').json()['scan']['first_confirmed_ms'] is None
        frame = SimpleNamespace(sequence=3, captured=time.monotonic(), image=image)
        result = until('first_confirmed_ms')
        assert result['latest']['value'] == 'HOLDER-42'
        assert result['first_confirmed_ms'] >= result['first_read_ms']
        assert result['count'] == 1
        rows = client.get('/api/scan-history').json()
        assert [r['stage'] for r in rows] == ['Confirmed', 'First read']
        assert rows[0]['trial_ms'] == result['first_confirmed_ms']
        assert rows[0]['frame_ms'] >= 0
        frame = SimpleNamespace(sequence=4, captured=time.monotonic(), image=image)
        for _ in range(100):
            if client.get('/api/scan-history?repeats=true').json()[0]['stage'] == 'Repeat':
                break
            time.sleep(.02)
        else:
            pytest.fail('No repeat reading logged')
        assert toggle(False) is None
        assert toggle(True)['first_confirmed_ms'] is None
        assert client.get('/api/scan-history').json() == rows


@pytest.mark.parametrize('x,fmt,path', [
    (340, zxingcpp.BarcodeFormat.DataMatrix, 'aiming box'),
    (15, zxingcpp.BarcodeFormat.DataMatrix, 'full frame'),
    (340, zxingcpp.BarcodeFormat.QRCode, 'full frame'),
])
def test_center_first_uses_native_coordinates_and_fallback(x, fmt, path):
    img = np.full((480, 800, 3), 255, np.uint8)
    code = barcode('H-2', fmt)
    h, w = code.shape[:2]
    img[143:143+h, x:x+w] = code
    pipe = Pipeline()
    cfg = Settings(barcode_mode='qr_and_data_matrix')
    first = pipe.process(img, cfg, center_first=True)
    item, = first['reads']
    assert path in item['pipeline']
    assert min(p[0] for p in item['points']) >= x
    assert not first['confirmed']
    confirmed, = pipe.process(img, cfg, center_first=True)['confirmed']
    assert confirmed['value'] == 'H-2'
