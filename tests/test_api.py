import importlib
import io
import time
import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from tests.test_vision import barcode


@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv('DPM_DATA',str(tmp_path))
    monkeypatch.delenv('DPM_TOKEN',raising=False)
    import backend.app as module
    module=importlib.reload(module)
    with TestClient(module.app) as test:
        yield test


def test_health_empty_state_and_settings_validation(client):
    assert client.get('/api/health').json()['service']=='DPM Scanner Lab'
    assert client.get('/api/health').json()['auth_required'] is False
    assert client.get('/api/settings').status_code == 200
    assert not client.cookies
    assert client.get('/api/holders').json()==[]
    analytics=client.get('/api/analytics').json()
    assert analytics['total']==0 and analytics['success_rate'] is None
    assert client.patch('/api/settings',json={'threshold_block':4}).status_code==422
    assert client.post('/api/camera/unknown').status_code==404


def test_live_exposure_changes_are_applied_before_saving(client, monkeypatch):
    import backend.app as module
    client.patch('/api/settings', json={'adapter': 'v4l2', 'device': 10})
    submitted = []
    monkeypatch.setattr(module, 'control_service', lambda values=None: submitted.append(values) or {'available': True})
    result = client.patch('/api/settings', json={'exposure_mode': 'manual', 'exposure_us': 5000, 'analog_gain': 2})
    assert result.status_code == 200
    assert submitted[-1]['exposure_us'] == 5000
    export = client.get('/api/export/calibration').json()
    assert export['settings']['exposure_mode'] == 'manual'
    assert export['settings']['focus_distance_mm'] == 101.6


def test_unavailable_camera_controls_do_not_save_claimed_settings(client, monkeypatch):
    import backend.app as module
    client.patch('/api/settings', json={'adapter': 'v4l2', 'device': 10})
    def unavailable(values=None):
        raise RuntimeError('Camera service unavailable')
    monkeypatch.setattr(module, 'control_service', unavailable)
    assert client.patch('/api/settings', json={'exposure_mode': 'manual'}).status_code == 409
    assert client.get('/api/settings').json()['exposure_mode'] == 'auto'


def test_live_webcam_format_is_not_claimed_to_change(client, monkeypatch):
    import backend.app as module
    client.patch('/api/settings', json={'adapter': 'v4l2', 'device': 10})
    monkeypatch.setattr(module, 'control_service', lambda values=None: {'available': True})
    assert client.patch('/api/settings', json={'width': 640}).status_code == 409
    assert client.get('/api/settings').json()['width'] == 1920


def test_isolated_inventory_and_manual_entries_do_not_confirm(client):
    result=client.post('/api/holders',json={'tool_id':'T1','holder_id':'H1'})
    assert result.status_code==200
    assert client.get('/api/holders').json()[0]['holder_id']=='H1'
    assert client.get('/api/scans').json()==[]
    client.delete(f"/api/holders/{result.json()['id']}")
    assert client.get('/api/holders').json()==[]


def test_upload_analysis_replay_and_path_safety(client):
    ok,encoded=cv2.imencode('.png',barcode())
    response=client.post('/api/media',files={'file':('holder.png',encoded.tobytes(),'image/png')})
    assert response.status_code==200
    name=response.json()['name']
    analysis=client.get(f'/api/analyze/{name}').json()
    assert analysis['decoded'][0]['value']=='HOLDER-001'
    assert analysis['stages']['clahe'].startswith('data:image/jpeg;base64,')
    assert client.get('/media/not-a-lab-file.png').status_code==404
    assert client.post('/api/media',files={'file':('unsafe.py',b'print(1)')}).status_code==422
    job=client.post('/api/replay',json={'files':[name],'presets':['Default','Reflective metal'],'expected':'HOLDER-001'}).json()['id']
    for _ in range(100):
        report=client.get('/api/replay/'+job).json()
        if report['state']!='running':
            break
        time.sleep(.05)
    assert report['state']=='complete'
    assert len(report['runs'])==2
    assert all(r['correct_frame_rate']==1 for r in report['runs'])
    assert client.get('/api/scans').json()==[]  # Replay never creates live confirmations.


def test_browser_adapter_single_reader_confirmation_and_session_logging(client):
    assert client.post('/api/camera/capture').status_code==409
    session=client.post('/api/sessions',json={'name':'Synthetic fixture test'}).json()['id']
    assert session>0
    client.patch('/api/settings',json={'adapter':'browser','confirmation_frames':2,'decoder_budget_ms':500})
    client.post('/api/camera/start')
    for _ in range(50):
        if client.get('/api/status').json()['camera']['state']=='RUNNING':break
        time.sleep(.02)
    assert client.patch('/api/settings',json={'width':640}).status_code==409
    client.post('/api/camera/start')  # Idempotent, no second reader.
    assert client.post('/api/trials',json={'expected':'HOLDER-001'}).status_code==200
    ok,img=cv2.imencode('.jpg',barcode())
    for _ in range(6):
        client.post('/api/frame',content=img.tobytes())
        time.sleep(.2)
    scans=client.get('/api/scans').json()
    assert len(scans)==1 and scans[0]['value']=='HOLDER-001'
    assert client.get('/api/analytics').json()['correct']==1
    assert client.get('/api/export/trials?format=csv').status_code==200
    assert client.post('/api/camera/diagnostic').json()['file'].endswith('.jpg')
    client.post('/api/camera/stop')
    assert client.get('/api/status').json()['camera']['state']=='STOPPED'


def test_video_replay_frame_stepping(client,tmp_path):
    image=barcode()
    h,w=image.shape[:2]
    path=tmp_path/'synthetic.avi'
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'MJPG'),10,(w,h))
    assert writer.isOpened()
    for _ in range(8):writer.write(image)
    writer.release()
    name=client.post('/api/media',files={'file':('synthetic.avi',path.read_bytes())}).json()['name']
    result=client.get(f'/api/analyze/{name}?frame=4').json()
    assert result['frame_count']==8 and result['frame']==4
    assert result['decoded'][0]['value']=='HOLDER-001'


def test_cross_origin_writes_blocked(client):
    assert client.post('/api/camera/start',headers={'Origin':'https://example.org'}).status_code==403


def test_lan_token_and_same_site_cookie(tmp_path,monkeypatch):
    monkeypatch.setenv('DPM_DATA',str(tmp_path))
    monkeypatch.setenv('DPM_TOKEN','isolated-test-token')
    import backend.app as module
    module=importlib.reload(module)
    with TestClient(module.app) as test:
        assert test.get('/api/health').status_code==200
        assert test.get('/api/settings').status_code==401
        assert test.post('/api/login',json={'token':'wrong'}).status_code==401
        response=test.post('/api/login',json={'token':'isolated-test-token'})
        assert response.status_code==200
        assert 'HttpOnly' in response.headers['set-cookie']
        assert 'SameSite=strict' in response.headers['set-cookie']
        assert test.get('/api/settings').status_code==200
