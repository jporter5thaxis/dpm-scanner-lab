import importlib.util
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('nvidia_update_plan',
    Path(__file__).parents[1]/'deploy/camera-r39/nvidia_update_plan.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
old = '39.2.0-20260601141651'
new = module.TARGET
installed = {'nvidia-l4t-core': old, 'nvidia-l4t-kernel': '6.8.12-tegra-'+old}


def test_coordinated_exact_patch():
    plan = f'Inst nvidia-l4t-core [{old}] ({new} L4T [arm64])\n'+\
           f'Inst nvidia-l4t-kernel [6.8.12-tegra-{old}] (6.8.12-tegra-{new} L4T [arm64])'
    assert module.checked_changes(plan, installed) == {
        'nvidia-l4t-core': new, 'nvidia-l4t-kernel': '6.8.12-tegra-'+new}


@pytest.mark.parametrize('line', [
    'Remv unrelated-package [1.0]',
    f'Inst openssl [1.0] ({new} Ubuntu [arm64])',
    f'Inst nvidia-l4t-camera ({new} L4T [arm64])',
    f'Inst nvidia-l4t-core [{old}] (39.3.0-1 L4T [arm64])',
    f'Inst nvidia-l4t-core [39.2.1-1] ({new} L4T [arm64])',
    'Inst malformed',
    '',
])
def test_changes_outside_reviewed_patch_are_rejected(line):
    with pytest.raises(ValueError):
        module.checked_changes(line, installed)
