import time
from types import SimpleNamespace

import pytest
import zxingcpp
from fastapi.testclient import TestClient

from camera_lab import app as lab
from tests.test_vision import barcode


@pytest.mark.parametrize('symbol', [zxingcpp.BarcodeFormat.QRCode, zxingcpp.BarcodeFormat.DataMatrix])
def test_timed_camera_scan(monkeypatch, symbol):
    image = barcode('HOLDER-42', symbol)
    frame = SimpleNamespace(sequence=1, captured=time.monotonic(), image=image)
    monkeypatch.setattr(lab, 'session', 'test')
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
        assert toggle(False) is None
        assert toggle(True)['first_confirmed_ms'] is None
