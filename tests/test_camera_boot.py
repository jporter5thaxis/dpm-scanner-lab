import runpy
from pathlib import Path
import pytest

configure = runpy.run_path(str(Path(__file__).resolve().parents[1]/'deploy/camera-r39/camera_boot_config.py'))['camera_boot_configuration']

BOOT = 'TIMEOUT 30\nDEFAULT primary\n\nLABEL primary\n  MENU LABEL original\n  LINUX /boot/Image\n  INITRD /boot/initrd\n  APPEND ${cbootargs} root=PARTUUID=original rw\n\n# Recovery comments remain\n'
OVERLAY = '/boot/tegra234-p3767-camera-p3768-imx219-dual.dtbo'

def test_camera_entry_retains_original_kernel_root_and_recovery_choice():
    updated = configure(BOOT, OVERLAY)
    assert BOOT.split('LABEL primary',1)[1] in updated
    assert 'DEFAULT dpm-imx219\n' in updated
    assert updated.count('APPEND ${cbootargs} root=PARTUUID=original rw') == 2
    assert updated.count('LINUX /boot/Image') == 2
    assert 'OVERLAYS '+OVERLAY in updated
    assert configure(updated, OVERLAY) == updated

def test_preserves_existing_dtb_and_other_hardware_overlays():
    original = BOOT.replace('  INITRD', '  FDT /boot/custom.dtb\n  OVERLAYS /boot/existing-gpio.dtbo\n  INITRD')
    updated = configure(original, OVERLAY)
    assert updated.count('FDT /boot/custom.dtb') == 2
    assert 'OVERLAYS /boot/existing-gpio.dtbo,'+OVERLAY in updated

def test_invalid_or_conflicting_boot_choice_is_rejected():
    with pytest.raises(ValueError):
        configure(BOOT.replace('DEFAULT primary','DEFAULT absent'), OVERLAY)
    with pytest.raises(ValueError):
        configure(BOOT.replace('  INITRD','  OVERLAYS /boot/other-imx219.dtbo\n  INITRD'), OVERLAY)

def test_explicit_camera_dtb_only_changes_the_new_boot_choice():
    original = BOOT.replace('  INITRD','  FDT /boot/original.dtb\n  INITRD')
    updated = configure(original, OVERLAY, '/boot/dpm-imx219-camera.dtb')
    assert 'FDT /boot/original.dtb' in updated.split('LABEL dpm-imx219')[0]
    assert 'FDT /boot/dpm-imx219-camera.dtb' in updated.split('LABEL dpm-imx219')[1]
    assert 'FDT /boot/original.dtb' not in updated.split('LABEL dpm-imx219')[1]

def test_baked_camera_dtb_preserves_recovery_and_gpio_without_applying_camera_twice():
    original = BOOT.replace('  INITRD','  OVERLAYS /boot/gpio.dtbo\n  INITRD')
    updated = configure(original, OVERLAY, '/boot/dpm-imx219-camera.dtb', baked=True)
    recovery, camera = updated.split('LABEL dpm-imx219')
    assert original.split('LABEL primary',1)[1] in recovery
    assert 'OVERLAYS /boot/gpio.dtbo' in camera
    assert OVERLAY not in camera
    assert 'APPEND ${cbootargs} root=PARTUUID=original rw dpm_camera=1' in camera
    assert configure(updated, OVERLAY, '/boot/dpm-imx219-camera.dtb', baked=True) == updated
    with pytest.raises(ValueError):
        configure(updated, OVERLAY, '/boot/different.dtb', baked=True)
