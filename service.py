"""Start/stop the local service without killing unrelated Python processes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def state(base):
    try:
        with OPENER.open(base+'/api/state', timeout=2) as response:
            data = json.load(response)
        if data.get('service') != 'binance-local-monitor':
            raise RuntimeError('Port is occupied by another service or an older monitor version.')
        return data
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return None


def post(base, path, data):
    request = urllib.request.Request(base+path, data=json.dumps(data).encode(), headers={'Content-Type':'application/json'})
    with OPENER.open(request, timeout=120) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['start','stop'])
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    base = 'http://127.0.0.1:'+str(args.port)
    current = state(base)
    if args.action == 'stop':
        if current is None:
            print('Service is already stopped.')
            return
        post(base, '/api/shutdown', {})
        print('Saving logs and stopping service...', flush=True)
        for _ in range(240):
            if state(base) is None:
                print('Service stopped. Saved logs are preserved.')
                return
            time.sleep(.5)
        raise RuntimeError('Still saving logs. Do not force-kill the process; check monitor-error.log.')
    if current is not None:
        post(base, '/api/control', {'action':'start'})
    else:
        import websocket  # Validate dependency before launching a hidden process.
        with (ROOT/'monitor.log').open('a',encoding='utf-8') as out, (ROOT/'monitor-error.log').open('a',encoding='utf-8') as err:
            child = subprocess.Popen([sys.executable,str(ROOT/'monitor.py'),'--port',str(args.port)], cwd=str(ROOT),
                                     stdout=out, stderr=err, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for _ in range(60):
            if child.poll() is not None:
                raise RuntimeError('Service failed to start. See monitor-error.log.')
            if state(base) is not None:
                break
            time.sleep(.5)
        else:
            raise RuntimeError('Service startup timed out. See monitor-error.log.')
    print('Service running: '+base)
    webbrowser.open(base)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('Error: '+str(exc),file=sys.stderr)
        sys.exit(1)
