#!/usr/bin/python3
"""Root-owned, fixed-path deployment helper; install only after operator approval."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path('/opt/slack-auto-mention')
INCOMING = Path('/var/lib/slack-auto-mention-incoming')
SERVICE = 'slack-auto-mention.service'


def verify_archive(archive, revision):
    expected = {'bot.py', 'revision.env'}
    with tarfile.open(fileobj=archive, mode='r:gz') as tar:
        entries = tar.getmembers()
        if len(entries) != 2 or {x.name for x in entries} != expected:
            raise ValueError('Invalid release contents')
        result = {}
        for member in entries:
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError('Invalid release entry')
            with tar.extractfile(member) as stream:
                result[member.name] = stream.read()
        if result['revision.env'] != f'APP_REVISION={revision}\n'.encode():
            raise ValueError('Revision mismatch')
        compile(result['bot.py'], 'bot.py', 'exec')
        return result


def switch(path):
    temporary = ROOT / 'next'
    # Never follow a user-controlled symlink when writing release contents.
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    temporary.symlink_to(path, target_is_directory=True)
    temporary.replace(ROOT / 'current')


def restart():
    subprocess.run(['systemctl', 'restart', SERVICE], check=True, timeout=45)


def healthy(revision):
    for _ in range(30):
        try:
            with urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=2) as response:
                data = json.load(response)
                if data.get('ready') is True and data.get('revision') == revision:
                    return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def activate(release, previous):
    switch(release)
    try:
        restart()
        if not healthy(release.name):
            raise RuntimeError('New revision health check failed')
    except Exception:
        if previous:
            switch(previous)
            restart()
            if not healthy(previous.name):
                raise SystemExit('Deployment and rollback health checks failed; inspect service')
            raise SystemExit('Deployment failed; previous revision restored and healthy')
        subprocess.run(['systemctl', 'stop', SERVICE], check=True, timeout=45)
        raise SystemExit('Initial deployment failed; service stopped; data preserved')


def main():
    import fcntl
    if os.geteuid() != 0 or len(sys.argv) != 3:
        raise SystemExit('Usage: approved root helper REVISION SHA256')
    revision, digest = sys.argv[1:]
    if not re.fullmatch('[a-f0-9]{40}', revision) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise SystemExit('Invalid release identity')
    # This root-owned directory and lock are provisioned during reviewed bootstrap.
    with open(ROOT / 'deploy.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive = INCOMING / (revision + '.tgz')
        # Read once with no symlink following; all verification operates on that snapshot.
        fd = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            import io
            raw = stream.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != digest:
            raise SystemExit('Archive checksum/size mismatch')
        files = verify_archive(io.BytesIO(raw), revision)
        releases = ROOT / 'releases'
        release = releases / revision
        if not release.exists():
            release.mkdir(mode=0o755)
            for name, data in files.items():
                path = release / name
                path.write_bytes(data)
                path.chmod(0o644)
        elif any((release / name).read_bytes() != data for name, data in files.items()):
            raise SystemExit('Existing immutable release differs')
        current = ROOT / 'current'
        previous = current.resolve(strict=True) if current.is_symlink() else None
        if previous and (previous.parent != releases or not re.fullmatch('[a-f0-9]{40}', previous.name)):
            raise SystemExit('Unrecognized previous release')
        activate(release, previous)
        print('Healthy deployed revision: ' + revision)


if __name__ == '__main__':
    main()
