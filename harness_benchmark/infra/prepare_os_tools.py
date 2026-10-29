"""Fetch the exact official grader tool; never run an Agent or read API keys."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
import urllib.request

from .common import HERE, ROOT, digest, read_json

TARGET = ROOT / '.tools/grader-bullseye/usr/bin/sshpass'


def prepare():
    pin = read_json(HERE / 'manifests/qemu-grader-tool.json')
    if TARGET.exists() and digest(TARGET) == pin['binary_sha256']:
        return TARGET
    with tempfile.TemporaryDirectory(prefix='tb2-sshpass-') as work:
        package = Path(work) / 'sshpass.deb'
        with urllib.request.urlopen(pin['source_url'], timeout=90) as source:
            package.write_bytes(source.read())
        if digest(package) != pin['package_sha256']:
            raise RuntimeError('Official sshpass package checksum differs')
        unpacked = Path(work) / 'unpacked'
        subprocess.run(['dpkg-deb', '-x', str(package), str(unpacked)], check=True)
        binary = unpacked / 'usr/bin/sshpass'
        if digest(binary) != pin['binary_sha256']:
            raise RuntimeError('Official sshpass binary checksum differs')
        TARGET.parent.mkdir(parents=True, exist_ok=True)
        TARGET.write_bytes(binary.read_bytes())
        TARGET.chmod(0o755)
    return TARGET


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    target = prepare()
    print('Pinned grader tool ready: ' + str(target) + ' sha256=' + digest(target))


if __name__ == '__main__':
    main()
