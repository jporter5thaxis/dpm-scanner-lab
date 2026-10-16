"""Run as a disposable subprocess on Linux; verifies the kernel write boundary."""
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from backend.sandbox import restrict_writes
root=Path(__file__).resolve().parent.parent
allowed=root/'data'
allowed.mkdir(exist_ok=True)
abi=restrict_writes(allowed)
good=allowed/'sandbox-test.txt'
good.write_text('allowed')
good.unlink()
outside=root/'forbidden-sandbox-test.txt'
try:
    outside.write_text('must be denied')
except PermissionError:
    print(f'PASS: Landlock ABI {abi} permits lab data and denies outside writes')
else:
    raise RuntimeError('Filesystem restriction failed')
