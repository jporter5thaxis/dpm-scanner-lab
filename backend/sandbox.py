"""Optional Linux Landlock boundary for an unprivileged scanner deployment.

Reads remain available to camera libraries. Filesystem writes are permitted only
within the scanner data directory and explicitly required device nodes. The
restriction is inherited by subprocesses and cannot be removed by the app.
"""
import ctypes
import os
from pathlib import Path
import platform


def restrict_writes(data: Path):
    if platform.system() != 'Linux' or platform.machine() not in ('aarch64','x86_64'):
        raise RuntimeError('DPM_RESTRICT_WRITES requires Linux ARM64 or x86_64')
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    def syscall(number, *args):
        result = libc.syscall(ctypes.c_long(number), *args)
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        return result

    abi = syscall(444, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 3:
        raise RuntimeError('Filesystem write protection requires Landlock ABI 3 or newer')

    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs',ctypes.c_uint64)]

    class PathRule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]

    # WRITE_FILE plus remove/make/refer/truncate bits; read and execute unrestricted.
    write_mask = (1 << 1) | sum(1 << bit for bit in range(4,15))
    ruleset = Ruleset(write_mask)
    descriptor = syscall(444, ctypes.byref(ruleset),ctypes.c_size_t(ctypes.sizeof(ruleset)),ctypes.c_uint(0))
    try:
        paths = [(data.resolve(),write_mask)]
        # Some drivers require read/write opens; never grant arbitrary /dev writes.
        devices = [Path('/dev/null'), *Path('/dev').glob('video[0-9]*'), *Path('/dev').glob('nvhost*'),
                   Path('/dev/nvmap'), Path('/dev/nvidiactl'), Path('/dev/nvidia-modeset'), *Path('/dev').glob('nvidia[0-9]*'),
                   Path('/dev/nvgpu/igpu0/ctrl'), Path('/dev/nvgpu/igpu0/power'),
                   *Path('/dev/dri').glob('renderD[0-9]*')]
        # Argus's EGL consumer opens GPU/render nodes read/write even when its
        # output goes to a pipe. Keep permissions limited to character devices.
        paths.extend((device,(1 << 1) | (1 << 14)) for device in devices if device.is_char_device())
        for path, allowed in paths:
            parent = os.open(path,os.O_PATH | os.O_CLOEXEC)
            try:
                rule=PathRule(allowed,parent)
                syscall(445,ctypes.c_int(descriptor),ctypes.c_int(1),ctypes.byref(rule),ctypes.c_uint(0))
            finally:
                os.close(parent)
        if libc.prctl(38,1,0,0,0) != 0:
            raise OSError(ctypes.get_errno(),'Could not enable no-new-privileges')
        syscall(446,ctypes.c_int(descriptor),ctypes.c_uint(0))
    finally:
        os.close(descriptor)
    return abi
