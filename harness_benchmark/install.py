"""Install frozen runtimes into this checkout, never the user's global CLI config."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

ROOT=Path(__file__).resolve().parents[1]
CACHE=ROOT/'.downloads'


def run(*args,**kwargs):
    subprocess.run(list(map(str,args)),check=True,**kwargs)


def archive(url):
    CACHE.mkdir(exist_ok=True)
    # Different release versions often share the same asset basename (uv).
    bucket=CACHE/hashlib.sha256(url.encode()).hexdigest()[:16]
    bucket.mkdir(exist_ok=True)
    path=bucket/url.rsplit('/',1)[1]
    if not path.exists():
        print('Downloading '+url,flush=True)
        temp=path.with_suffix(path.suffix+'.partial')
        request=urllib.request.Request(url,headers={'User-Agent':'harness-benchmark-installer'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request,timeout=120) as source,temp.open('wb') as target:shutil.copyfileobj(source,target)
                temp.replace(path)
                break
            except OSError as error:
                print(f'Download attempt {attempt+1} failed: {type(error).__name__}',flush=True)
                if attempt==2:raise
    return path


def unpack(url,target,strip=1):
    target.mkdir(parents=True,exist_ok=True)
    with tarfile.open(archive(url)) as stream:
        for member in stream.getmembers():
            parts=Path(member.name).parts[strip:]
            if not parts:continue
            if '..' in parts or Path(*parts).is_absolute():raise ValueError('Unsafe archive entry')
            member.name=str(Path(*parts));stream.extract(member,target,filter='data')


def install_uv(version,target):
    metadata=json.loads(archive(f'https://pypi.org/pypi/uv/{version}/json').read_text())
    candidates=[r for r in metadata['urls'] if 'manylinux_2_17_x86_64' in r['filename'] and 'musllinux' not in r['filename']]
    if len(candidates)!=1:raise RuntimeError('Cannot identify the frozen uv Linux x64 wheel')
    release=candidates[0];wheel=archive(release['url'])
    if hashlib.file_digest(wheel.open('rb'),'sha256').hexdigest()!=release['digests']['sha256']:
        raise RuntimeError('uv wheel checksum mismatch')
    target.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(wheel) as stream:
        for executable in ['uv','uvx']:
            member=next(n for n in stream.namelist() if n.endswith('/scripts/'+executable))
            path=target/executable;path.write_bytes(stream.read(member));path.chmod(0o755)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--appworld',action='store_true',help='Also install the isolated AppWorld Python environment');parser.add_argument('--skip-binary-hash-check',action='store_true',help='Creates a different runtime cohort; never use to claim bitwise reproduction')
    args=parser.parse_args()
    binary=ROOT/'.runtime/bin';binary.mkdir(parents=True,exist_ok=True)
    if not (binary/'node').exists():
        temp=CACHE/'node';unpack('https://nodejs.org/dist/v24.9.0/node-v24.9.0-linux-x64.tar.xz',temp)
        shutil.copy2(temp/'bin/node',binary/'node')
        # npm's CLI needs the node-relative package tree.
        npmroot=ROOT/'.tools/node';shutil.copytree(temp,npmroot,dirs_exist_ok=True)
    node_tools=ROOT/'.tools/node';npm=node_tools/'lib/node_modules/npm/bin/npm-cli.js'
    if not npm.exists():
        unpack('https://nodejs.org/dist/v24.9.0/node-v24.9.0-linux-x64.tar.xz',node_tools)
    for version,dest in [('0.7.13',binary),('0.9.5',binary/'uv095')]:
        if not (dest/'uv').exists():install_uv(version,dest)
    if not (binary/'rg').exists():
        temp=CACHE/'rg';unpack('https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-unknown-linux-musl.tar.gz',temp)
        shutil.copy2(temp/'rg',binary/'rg')
    python=ROOT/'.runtime/python/cpython-3.12.15-linux-x86_64-gnu'
    if not (python/'bin/python3.12').exists():
        unpack('https://github.com/astral-sh/python-build-standalone/releases/download/20261009/cpython-3.12.15%2B20261009-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz',python)
    python13=ROOT/'.runtime/python/cpython-3.13.5-linux-x86_64-gnu'
    if not (python13/'bin/python3.13').exists():
        unpack('https://github.com/astral-sh/python-build-standalone/releases/download/20250612/cpython-3.13.5%2B20250612-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz',python13)
    # A target must exist for Docker to bind the up-to-date command over the
    # read-only bin directory; each AppWorld trial mounts the source file.
    (ROOT/'harness_benchmark/appworld_command.py').chmod(0o755)
    shutil.copy2(ROOT/'harness_benchmark/appworld_command.py',binary/'appworld')
    (binary/'appworld').chmod(0o755)
    environment={**os.environ,'PATH':str(binary)+':'+os.environ.get('PATH','')}
    for directory in [ROOT/'vendor/npm',ROOT/'vendor/dsh']:
        run(binary/'node',npm,'ci','--prefix',directory,env=environment)
    codexpkg=ROOT/'.tools/codex'
    run(binary/'node',npm,'install','--prefix',codexpkg,'--no-audit','--no-fund','--save-exact','@openai/codex@0.163.0',env=environment)
    candidates=[p for p in (codexpkg/'node_modules').rglob('codex') if p.is_file() and p.read_bytes()[:4]==b'\x7fELF']
    if len(candidates)!=1:raise RuntimeError('Cannot identify the single Linux x64 Codex executable')
    shutil.copy2(candidates[0],binary/'codex')
    lock=json.loads((ROOT/'configs/runtime-binaries.sha256.json').read_text())
    actual={p:hashlib.file_digest((ROOT/p).open('rb'),'sha256').hexdigest() for p in lock}
    (ROOT/'runtime-installed.sha256.json').write_text(json.dumps(actual,indent=2)+'\n')
    differences=[p for p in lock if actual[p]!=lock[p]]
    if differences and not args.skip_binary_hash_check:
        raise RuntimeError('Runtime differs from experiment binary hashes: '+', '.join(differences))
    run(binary/'uv095/uv','venv','--allow-existing',ROOT/'.venv-harbor','--python',python/'bin/python3.12')
    run(binary/'uv095/uv','pip','sync','--python',ROOT/'.venv-harbor/bin/python',ROOT/'configs/harbor.requirements.lock.txt')
    for executable in [binary/'node',binary/'codex',binary/'uv',binary/'uv095/uv',binary/'rg']:run(executable,'--version')
    run(python/'bin/python3.12','--version');run(python13/'bin/python3.13','--version')
    if args.appworld:
        run(binary/'uv095/uv','venv','--allow-existing',ROOT/'.venv-appworld','--python',python/'bin/python3.12')
        run(binary/'uv095/uv','pip','sync','--python',ROOT/'.venv-appworld/bin/python',ROOT/'configs/appworld.requirements.lock.txt')
    print('Pinned runtime ready. No provider credential was read.')


if __name__=='__main__':main()
