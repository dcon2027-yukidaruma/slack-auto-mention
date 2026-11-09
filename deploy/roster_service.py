"""Private host Unix reader. No Slack token, public listener or credential copies."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import stat
import struct
import sys
import threading
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deploy.roster_reader import FreshRosterReader, batch_url

METADATA_FIELDS = 'spreadsheetId,sheets(properties(sheetId,title,hidden,gridProperties(rowCount,columnCount)))'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
READ_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'


def private_json(path):
    if path.is_symlink() or path.resolve(strict=True) != path.absolute():
        raise ValueError('reader_private_path_invalid')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('reader_private_parent_invalid')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_size > 16384):
            raise ValueError('reader_private_file_invalid')
        value = json.loads(stream.read(16385))
    if not isinstance(value, dict):
        raise ValueError('reader_private_schema_invalid')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):return None


class GoogleRequests:
    def __init__(self, source_id):
        self.source_id = source_id
        self.api = 'https://sheets.googleapis.com/v4/spreadsheets/' + source_id
        self.metadata_url = self.api + '?' + urllib.parse.urlencode({'fields': METADATA_FIELDS})

    @staticmethod
    def _http(url, *, token=None, data=None):
        try:
            headers = {'Accept': 'application/json'}
            if token:headers['Authorization'] = 'Bearer ' + token
            if data is not None:headers['Content-Type'] = 'application/x-www-form-urlencoded'
            request = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.build_opener(NoRedirect).open(request, timeout=4) as response:
                raw = response.read(131073)
            if len(raw) > 131072:raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict):raise ValueError()
            return value
        except Exception:
            raise ValueError('google_read_unavailable') from None

    def __call__(self, url, *, token=None, data=None):
        if not (url == TOKEN_URL and data is not None and token is None
                or url == self.metadata_url and data is None and isinstance(token, str) and token):
            raise ValueError('reader_endpoint_rejected')
        return self._http(url, token=token, data=data)

    def batch(self, source_id, ranges, *, token):
        if source_id != self.source_id or not isinstance(token, str) or not token:
            raise ValueError('reader_batch_source_rejected')
        return self._http(batch_url(source_id, ranges), token=token)


def build_existing_reader(config, *, home=None):
    """Use one explicitly pinned existing host module and key; never discover secrets."""
    home = home or Path.home()
    if (set(config) != {'source_id', 'key_id', 'reader_runtime', 'reader_module_sha256'}
            or not re.fullmatch('[A-Za-z0-9_-]{10,100}', config['source_id'])
            or not re.fullmatch('[a-f0-9]{40}', config['key_id'])
            or any(not re.fullmatch('[a-f0-9]{64}', config[k]) for k in ['reader_runtime', 'reader_module_sha256'])):
        raise ValueError('reader_configuration_invalid')
    root = home / '.local/share/dcon-projects-runtime/releases' / config['reader_runtime']
    if root.is_symlink() or root.resolve(strict=True) != root.absolute():
        raise ValueError('reader_dependency_path_invalid')
    manifest = json.loads((root / 'MANIFEST.sha256.json').read_bytes())
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    module = root / 'sheet_sync.py'
    if (digest != root.name or module.is_symlink()
            or hashlib.sha256(module.read_bytes()).hexdigest() != config['reader_module_sha256']
            or manifest.get('sheet_sync.py') != config['reader_module_sha256']):
        raise ValueError('reader_dependency_unverified')
    spec = importlib.util.spec_from_file_location('approved_host_sheet_reader', module)
    dependency = importlib.util.module_from_spec(spec);spec.loader.exec_module(dependency)
    if (dependency.SHEET != config['source_id'] or dependency.SCOPE != READ_SCOPE
            or dependency.TOKEN_URL != TOKEN_URL):
        raise ValueError('reader_dependency_scope_mismatch')
    key = dependency.validate_key(home / '.config/dcon-projects/sheet-reader-rsa2048')
    requests = GoogleRequests(config['source_id'])
    existing = dependency.SheetReader(config['key_id'], key, request=requests)
    return FreshRosterReader(existing, requests.batch, source_id=config['source_id'])


def dispatch(reader, raw, *, peer_uid, allowed_uid):
    if peer_uid != allowed_uid or len(raw) > 4096 or not raw.endswith(b'\n'):
        return b'{"status":"held"}\n'
    try:
        value = reader.handle(json.loads(raw))
        result = json.dumps(value, separators=(',', ':')).encode() + b'\n'
        if len(result) > 131072:raise ValueError()
        return result
    except Exception:
        return b'{"status":"held"}\n'


def serve(reader, directory, *, allowed_uid):
    import fcntl
    if allowed_uid != os.getuid() or allowed_uid == 0:
        raise ValueError('reader_requires_existing_nonroot_owner')
    if (directory.is_symlink() or directory.resolve(strict=True) != directory.absolute()
            or directory.stat().st_uid != allowed_uid or stat.S_IMODE(directory.stat().st_mode) != 0o700):
        raise ValueError('reader_socket_directory_invalid')
    path = directory / 'reader.sock'
    lock_fd = os.open(directory / 'reader.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'r+') as lock:
        if os.fstat(lock.fileno()).st_uid != allowed_uid or stat.S_IMODE(os.fstat(lock.fileno()).st_mode) != 0o600:
            raise ValueError('reader_lock_invalid')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != allowed_uid:
                raise ValueError('reader_socket_collision')
            path.unlink()  # Only our stale socket, protected by the exclusive owner lock.

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                try:
                    peer = self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                    _, uid, _ = struct.unpack('3i', peer)
                    if uid != allowed_uid:return
                    raw = self.rfile.readline(4097)
                    self.wfile.write(dispatch(reader, raw, peer_uid=uid, allowed_uid=allowed_uid))
                except Exception:pass  # Never log authentication responses or raw requests.

        class Server(socketserver.UnixStreamServer):
            request_queue_size = 4
            def handle_error(self, *_args):pass

        with Server(str(path), Handler) as server:
            os.chmod(path, 0o600);inode = path.stat().st_ino
            def shutdown(*_args):threading.Thread(target=server.shutdown, daemon=True).start()
            signal.signal(signal.SIGTERM, shutdown);signal.signal(signal.SIGINT, shutdown)
            try:server.serve_forever(poll_interval=0.5)
            finally:
                if path.exists() and path.lstat().st_ino == inode and stat.S_ISSOCK(path.lstat().st_mode):path.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        reader = build_existing_reader(private_json(args.config))
        serve(reader, Path.home() / '.local/state/slack-roster-reader', allowed_uid=os.getuid())
    except Exception:
        raise SystemExit('Private roster reader unavailable; details suppressed') from None


if __name__ == '__main__':main()
