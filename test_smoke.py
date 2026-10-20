"""Isolated API/archive check. Requires app requirements; no real server data touched."""
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
RUNNER = '''
import os, sys
import config
from pathlib import Path
root = Path(sys.argv[1])
for name in ['INCOMING_DIR', 'WEB_BACKUPS_DIR', 'SESSION_LOG_DIR']:
    path = root / name
    path.mkdir()
    setattr(config, name, str(path))
config.AUTHORIZED_KEYS_PATH = str(root / 'authorized_keys')
config.BACKUP_UID = os.getuid()
config.BACKUP_GID = os.getgid()
config.ensure_ssh_wrapper = lambda: None
import main, uvicorn
uvicorn.run(main.app, fd=int(sys.argv[2]), log_level='error', access_log=False)
'''


def main():
    # Use the checkout filesystem: /tmp may be a tmpfs below the API's 1 GiB guard.
    with tempfile.TemporaryDirectory(prefix='backuper-smoke-', dir=ROOT) as tmp, socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(16)
        base = f'http://127.0.0.1:{listener.getsockname()[1]}'
        process = subprocess.Popen([sys.executable, '-c', RUNNER, tmp, str(listener.fileno())],
                                   cwd=ROOT / 'rootfs/opt/backup-api', pass_fds=(listener.fileno(),))
        def request(path, fields=None):
            data = None if fields is None else urllib.parse.urlencode(fields).encode()
            try:
                with urllib.request.urlopen(base + path, data=data, timeout=3) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
        try:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError('Sandbox API failed to start')
                try:
                    if request('/disk_space')[0] == 200:
                        break
                except (OSError, TimeoutError):
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Sandbox API startup timeout')
            assert request('/agent.sh')[0] == 200
            assert request('/clear_backups', {})[0] == 403
            assert request('/status/not-a-uuid')[0] == 400
            invalid = request('/register_key', {'key': 'not-an-ssh-key'})
            assert invalid[0] == 400, invalid
            assert request('/register_key', {'key': 'not-an-ssh-key'})[0] == 429
            # Registration uses a hardcoded production authorized_keys path.
            # Seed a sandbox session instead; live rsync registration is covered by backup-probe.
            session = str(uuid.uuid4())
            (Path(tmp) / 'authorized_keys').write_text('sandbox-key # ' + session + '\n')
            (Path(tmp) / 'INCOMING_DIR' / session).mkdir()
            payload = Path(tmp) / 'INCOMING_DIR' / session / 'hello.txt'
            payload.write_text('restore-check\n')
            assert request('/create_archive', {'session_id': session})[0] == 200
            for _ in range(100):
                status, body = request('/status/' + session)
                if status == 200 and body.startswith(b'\x1f\x8b'):
                    break
                time.sleep(.1)
            else:
                raise RuntimeError('Sandbox archive timeout')
            with tarfile.open(fileobj=io.BytesIO(body), mode='r:gz') as archive:
                assert archive.extractfile('backup/hello.txt').read() == b'restore-check\n'
            assert session not in (Path(tmp) / 'authorized_keys').read_text()
            assert not payload.parent.exists()
            assert request('/cancel_session', {'session_id': session})[0] == 200
            print('PASS: isolated API, input validation, rate limit, key cleanup, archive and restored payload')
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    main()
