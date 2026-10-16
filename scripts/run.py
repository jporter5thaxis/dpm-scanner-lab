"""Safe local runner: one worker, loopback by default, no camera auto-start."""
import argparse
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
parser = argparse.ArgumentParser()
parser.add_argument('--host',default='127.0.0.1')
parser.add_argument('--port',type=int,default=8095)
args = parser.parse_args()
probe = socket.socket()
if os.name == 'posix':
    # Allow restart during TCP TIME_WAIT while still rejecting an active listener.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    probe.bind((args.host,args.port))
except OSError:
    parser.error(f'Port {args.port} is occupied. Choose a free --port; existing services will not be changed.')
finally:
    probe.close()
import uvicorn
if os.environ.get('DPM_RESTRICT_WRITES') == '1':
    from backend.sandbox import restrict_writes
    scanner_data = Path(os.environ.get('DPM_DATA', Path(__file__).resolve().parent.parent/'data')).resolve()
    scanner_data.mkdir(parents=True,exist_ok=True)
    scanner_temp = scanner_data/'tmp'
    scanner_temp.mkdir(exist_ok=True)
    os.environ['TMPDIR'] = str(scanner_temp)
    os.environ['GST_REGISTRY'] = str(scanner_data/'gstreamer-registry.bin')
    os.environ['XDG_CACHE_HOME'] = str(scanner_data/'cache')
    import tempfile
    tempfile.tempdir = str(scanner_temp)
    sys.dont_write_bytecode = True
    abi = restrict_writes(scanner_data)
    print(f'Scanner filesystem writes restricted (Landlock ABI {abi})',flush=True)
uvicorn.run('backend.app:app',host=args.host,port=args.port,workers=1,log_level='info')
