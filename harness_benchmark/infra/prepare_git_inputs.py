"""Pin the public upstream repository that the Cython grader itself downloads."""
import argparse
import hashlib
from pathlib import Path
import subprocess

from .common import DEFAULT_OUT,now,save_json

URL='https://github.com/SPOCKnots/pyknotid.git'
TAG='0.5.3'
COMMIT='441c807dbec2ee32e1da572e24e58d52a4eb7afa'
CONTAINER_PATH='/opt/bench/grader-git/pyknotid.git'


def environment():
    return dict(GIT_CONFIG_COUNT='3',
        GIT_CONFIG_KEY_0='url.file://'+CONTAINER_PATH+'.insteadOf',GIT_CONFIG_VALUE_0=URL,
        GIT_CONFIG_KEY_1='protocol.file.allow',GIT_CONFIG_VALUE_1='always',
        GIT_CONFIG_KEY_2='safe.directory',GIT_CONFIG_VALUE_2=CONTAINER_PATH)


def verify(cache):
    def git(*args):
        return subprocess.check_output(['git','--git-dir',str(cache),*args],stderr=subprocess.PIPE)
    assert git('rev-parse','refs/tags/'+TAG).decode().strip()==COMMIT
    assert git('rev-parse','HEAD').decode().strip()==COMMIT
    git('fsck','--strict')
    tree=git('rev-parse',COMMIT+'^{tree}').decode().strip()
    sha=hashlib.sha256(git('archive','--format=tar',COMMIT)).hexdigest()
    return dict(url=URL,tag=TAG,commit=COMMIT,tree=tree,canonical_archive_sha256=sha,
        model_calls=0,scope='Public upstream grader input only; not benchmark solution or hidden tests')


def prepare(out):
    cache=Path(out)/'dependencies/grader-git/pyknotid.git'
    cache.parent.mkdir(parents=True,exist_ok=True)
    if not cache.exists():
        subprocess.run(['timeout','-k','5','180','git','-c','http.version=HTTP/1.1',
            'clone','--bare','--depth','1','--single-branch','--branch',TAG,URL,str(cache)],check=True)
    proof=verify(cache)
    save_json(Path(out)/'preflight/cython-grader-input.json',dict(prepared_at=now(),**proof))
    return cache,proof


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,default=DEFAULT_OUT)
    args=parser.parse_args();_,proof=prepare(args.output.resolve())
    print('Pinned grader repository verified: '+proof['commit'])


if __name__=='__main__':main()
