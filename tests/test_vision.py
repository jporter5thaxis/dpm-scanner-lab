import cv2
import numpy as np
import pytest
import zxingcpp
from backend.config import ConfigStore, Settings
from backend.vision import Pipeline, Validator, rectify, quality, stages


def barcode(value='HOLDER-001', symbology=None):
    code = zxingcpp.create_barcode(value, symbology or zxingcpp.BarcodeFormat.DataMatrix)
    pixels = np.asarray(code.to_image(scale=8, add_quiet_zones=True))
    return cv2.cvtColor(pixels, cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize('symbology',[zxingcpp.BarcodeFormat.QRCode,zxingcpp.BarcodeFormat.DataMatrix])
@pytest.mark.parametrize('rotate',[0,1,2,3])
def test_real_decoder_reads_qr_and_data_matrix_at_rotations(symbology,rotate):
    image = np.ascontiguousarray(np.rot90(barcode(symbology=symbology),rotate))
    pipeline = Pipeline()
    cfg = Settings(barcode_mode="qr_and_data_matrix", confirmation_frames=1, decoder_budget_ms=500)
    result = pipeline.process(image,cfg)
    assert [r['value'] for r in result['confirmed']] == ['HOLDER-001']
    assert result['confirmed'][0]['symbology'] == ('QR Code' if symbology==zxingcpp.BarcodeFormat.QRCode else 'Data Matrix')


def test_independent_frames_duplicates_absence_and_payload_rules():
    v = Validator()
    cfg = Settings(confirmation_frames=2,prefix='H-',expected_length=3)
    assert not v.accept('BAD','QR Code',1,0,cfg)[0]
    assert not v.accept('H-1','QR Code',1,0,cfg)[0]
    assert not v.accept('H-1','QR Code',1,.1,cfg)[0]
    assert v.accept('H-1','QR Code',2,.2,cfg)[0]
    assert not v.accept('H-1','QR Code',3,.3,cfg)[0]
    assert not v.accept('H-1','QR Code',4,4,cfg)[0]
    assert v.accept('H-1','QR Code',5,4.1,cfg)[0]
    assert not v.accept('H-2','Code 128',6,5,cfg)[0]


def test_full_frame_code_far_from_center_and_no_confirm_without_decode():
    small = barcode()
    canvas = np.full((900,1500,3),255,np.uint8)
    canvas[30:30+small.shape[0],1100:1100+small.shape[1]] = small
    p = Pipeline()
    cfg = Settings(confirmation_frames=1,decoder_budget_ms=500)
    report = p.process(canvas,cfg)
    assert report['confirmed'][0]['value']=='HOLDER-001'
    assert report['tracks'][0]['points'][0][0]>1000
    report = p.process(np.full_like(canvas,255),cfg)
    assert not report['confirmed']


def test_overlay_coordinates_and_perspective_crop():
    image = np.zeros((480,640,3),np.uint8)
    image[100:300,200:400]=255
    points = np.float32([[200,100],[400,100],[400,300],[200,300]])
    crop = rectify(image,points)
    assert crop.shape[:2]==(200,200)
    assert crop.mean()>250
    p=Pipeline()
    t=p.associate(points,0)
    assert t.id==1
    assert p.associate(points+2,.1).id==1


def test_config_atomic_persistence_and_invalid_kernel(tmp_path):
    store=ConfigStore(tmp_path)
    store.update({'gamma':.7})
    store.save_preset('Close focus')
    reopened=ConfigStore(tmp_path)
    assert reopened.settings.gamma==.7
    assert reopened.presets()['Close focus']['gamma']==.7
    with pytest.raises(ValueError):
        store.update({'threshold_block':20})


def test_actual_processing_stages_and_quality():
    img=barcode()
    outputs=stages(img,Settings())
    assert set(outputs)=={'raw','grayscale','normalized','clahe','filtered','sharpened','adaptive','binary','inverted'}
    assert np.array_equal(outputs['inverted'],255-outputs['binary'])
    assert quality(img)['sharpness']>quality(cv2.GaussianBlur(img,(15,15),4))['sharpness']


def test_tracked_prediction_does_not_confirm():
    p=Pipeline()
    cfg=Settings(confirmation_frames=2,decoder_budget_ms=500)
    result=p.process(barcode(),cfg)
    assert not result['confirmed']
    result=p.process(barcode(),cfg.model_copy(update={'auto_decode':False}))
    assert not result['confirmed']


def test_consecutive_frame_confirmation_and_suppression():
    p=Pipeline()
    cfg=Settings(confirmation_frames=2,decoder_budget_ms=500)
    assert not p.process(barcode(),cfg)['confirmed']
    assert p.process(barcode(),cfg)['confirmed']
    assert not p.process(barcode(),cfg)['confirmed']


def test_successful_raw_decode_does_not_build_unused_enhancements(monkeypatch):
    import backend.vision as module
    def unused(*args):
        raise AssertionError('Raw decode succeeded; enhancement work is unnecessary')
    monkeypatch.setattr(module, 'stages', unused)
    pipeline = Pipeline()
    monkeypatch.setattr(pipeline, 'discover', lambda *args: [])
    result = pipeline.process(barcode(), Settings(confirmation_frames=1, decoder_budget_ms=500))
    assert result['confirmed'][0]['value'] == 'HOLDER-001'


def test_enhanced_decode_fallback_remains_available(monkeypatch):
    import backend.vision as module
    decode = module.zxingcpp.read_barcodes
    calls = []
    def raw_then_decode(image, **kwargs):
        calls.append(image)
        return [] if len(calls) == 1 else decode(image, **kwargs)
    monkeypatch.setattr(module.zxingcpp, 'read_barcodes', raw_then_decode)
    result = Pipeline().process(barcode(), Settings(confirmation_frames=1, decoder_budget_ms=500))
    assert result['confirmed'][0]['value'] == 'HOLDER-001'
    assert 'clahe' in result['confirmed'][0]['pipeline']
