import pytest
from backend.camera_controls import DEFAULT_CONTROLS, property_requests, validate_controls


def test_manual_exposure_converts_microseconds_and_fixes_gain():
    values = {**DEFAULT_CONTROLS, 'exposure_mode': 'manual', 'exposure_us': 5000, 'analog_gain': 2}
    result = property_requests(values)
    assert result['exposuretimerange'] == '5000000 5000000'
    assert result['gainrange'] == '2 2'
    assert result['ispdigitalgainrange'] == '1 1'
    assert result['aelock'] is False


@pytest.mark.parametrize('changes', [
    {'exposure_us': 33334}, {'exposure_us': 0}, {'analog_gain': 11},
    {'analog_gain': float('nan')}, {'exposure_compensation': float('inf')},
    {'white_balance': 9}, {'white_balance': True}, {'exposure_mode': 'custom'},
    {'pipeline': 'untrusted command'},
])
def test_controls_reject_unsafe_or_unsupported_values(changes):
    with pytest.raises(ValueError):
        validate_controls({**DEFAULT_CONTROLS, **changes})


def test_auto_preserves_sensor_ranges_and_compensation():
    result = property_requests({**DEFAULT_CONTROLS, 'exposure_compensation': -1})
    assert result['exposuretimerange'] == '13000 33333000'
    assert result['gainrange'] == '1 10.625'
    assert result['exposurecompensation'] == -1
