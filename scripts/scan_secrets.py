"""Scan tracked release files without printing any matching content."""
import pathlib
import re
import subprocess
import sys

PATTERNS = [
    r'xox[baprs]-[A-Za-z0-9-]{10,}',
    r'xapp-[A-Za-z0-9-]{10,}',
    r'gh[pousr]_[A-Za-z0-9]{20,}',
    r'github_pat_[A-Za-z0-9_]{20,}',
    r'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----',
    r'AKIA[A-Z0-9]{16}',
    r'(?i)(?:token|password|secret)\s*[:=]\s*[\x22\x27][A-Za-z0-9/+_-]{24,}[\x22\x27]',
    # Real workspace/recipient IDs belong only in private configuration.
    r'[TUCG][A-Z0-9]{10,}',
    r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',
]


def main():
    files = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    bad = []
    for name in filter(None, files):
        path = pathlib.Path(name)
        if path.name.startswith('.env') and path.name != '.env.example':
            bad.append(name)
            continue
        if path.suffix in ('.sqlite3', '.pem', '.key') or '.ssh' in path.parts:
            bad.append(name)
            continue
        try:
            data = path.read_text(encoding='utf-8')
        except (UnicodeError, OSError):
            bad.append(name)
            continue
        if any(re.search(pattern, data) for pattern in PATTERNS):
            bad.append(name)
    if bad:
        print('Potential secret/private identifier in: ' + ', '.join(bad))
        return 1
    print(f'Secret/private-identifier scan passed ({len(list(filter(None, files)))} tracked files).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
