"""Bounded IMX219 capture controls shared with the isolated camera service."""
import math

DEFAULT_CONTROLS = {'exposure_mode': 'auto', 'exposure_us': 10000.0,
                    'analog_gain': 1.0, 'exposure_compensation': 0.0,
                    'white_balance': 1}
CONTROL_KEYS = frozenset(DEFAULT_CONTROLS)


def validate_controls(values):
    if not isinstance(values, dict) or set(values) != CONTROL_KEYS:
        raise ValueError('Supply the complete supported camera control set')
    result = dict(values)
    if result['exposure_mode'] not in ('auto', 'manual'):
        raise ValueError('Exposure mode must be auto or manual')
    for key, minimum, maximum in [('exposure_us', 13, 33333),
                                  ('analog_gain', 1, 10.625),
                                  ('exposure_compensation', -2, 2)]:
        value = result[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
            raise ValueError(f'{key} must be between {minimum} and {maximum}')
    balance = result['white_balance']
    if isinstance(balance, bool) or not isinstance(balance, int) or not 0 <= balance <= 8:
        raise ValueError('Unsupported white balance mode')
    return result


def property_requests(values):
    values = validate_controls(values)
    manual = values['exposure_mode'] == 'manual'
    exposure = round(values['exposure_us'] * 1000)
    gain = values['analog_gain']
    return {'exposuretimerange': f'{exposure} {exposure}' if manual else '13000 33333000',
            'gainrange': f'{gain:g} {gain:g}' if manual else '1 10.625',
            'ispdigitalgainrange': '1 1', 'aelock': False,
            'exposurecompensation': 0.0 if manual else values['exposure_compensation'],
            'wbmode': values['white_balance']}
