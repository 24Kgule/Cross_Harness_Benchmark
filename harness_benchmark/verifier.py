"""Official tests with a pinned, cached dependency bootstrap.

Only two recognized uv-installer shell blocks are substituted. All test code,
arguments, package version requirements and reward logic are byte-identical.
The model cannot see /tests until Harbor invokes this verifier.
"""
import difflib
import copy
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import tempfile

from harbor.verifier.verifier import Verifier
from harbor.models.task.paths import TaskPaths
from .infra.dependency_policy import grading_indexes,dataset_transport
from .common import command
from .infra.cache_copy import SCRIPT as CACHE_COPY_SCRIPT

LOADED_SOURCE_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def bootstrap_script(source):
    # QEMU's extra package is provided from its image's exact APT metadata.
    # curl was used only for the uv installer, also replaced by pinned uv.
    source_qemu,nq=re.subn(r'# Install curl\napt-get update\napt-get install -y curl sshpass\n',
        '# Pinned grader bootstrap: checksum-verified sshpass provisioned after Agent stop.\n',source)
    if nq>1:raise ValueError('Ambiguous QEMU package bootstrap')
    source=source_qemu
    pattern=r'# Install curl\napt-get update\napt-get install -y curl\n\n# Install uv\ncurl -LsSf https://astral.sh/uv/0\.9\.5/install\.sh \| sh\n\nsource \$HOME/\.local/bin/env'
    changed,n=re.subn(pattern,'# Pinned bootstrap: uv 0.9.5 is mounted by the benchmark.\nexport PATH=/opt/bench/bin/uv095:$PATH',source)
    if not n:
        pattern=r'curl -LsSf https://astral.sh/uv/0\.9\.5/install\.sh \| sh\s*\nsource \$HOME/\.local/bin/env'
        changed,n=re.subn(pattern,'# Pinned bootstrap: uv 0.9.5 is mounted by the benchmark.\nexport PATH=/opt/bench/bin/uv095:$PATH',source)
    if not n and 'astral.sh' not in source:return source
    if n!=1:raise ValueError('Unrecognized verifier bootstrap; do not alter official tests')
    return changed


class TerminalVerifier(Verifier):
    async def _preserve_agent_filesystem(self,cid):
        (self.trial_paths.verifier_dir/'source-container.json').write_text(json.dumps(dict(
            container_id=cid,model_calls=0,phase='Agent stopped before grading')))

    def _resolve_tests(self):
        dirs,source,script=super()._resolve_tests()
        if len(dirs)!=1 or source!=dirs[0]:raise ValueError('Expected original single-step TB2 tests')
        self._bootstrap_tmp=tempfile.TemporaryDirectory(prefix='.tb2-grader-',dir=self.trial_paths.verifier_dir)
        target=Path(self._bootstrap_tmp.name)/'tests';shutil.copytree(source,target)
        original=script.read_text();changed=bootstrap_script(original)
        (target/script.relative_to(source)).write_text(changed)
        evidence=self.trial_paths.verifier_dir
        evidence.mkdir(parents=True,exist_ok=True)
        (evidence/'bootstrap.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True),changed.splitlines(True),fromfile='official/test.sh',tofile='pinned-bootstrap/test.sh')))
        checks={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file() and p!=script}
        assert all(hashlib.sha256((target/p).read_bytes()).hexdigest()==sha for p,sha in checks.items())
        (evidence/'bootstrap-provenance.json').write_text(json.dumps(dict(test_files_sha256=checks,
            original_script_sha256=hashlib.sha256(original.encode()).hexdigest(),
            bootstrap_script_sha256=hashlib.sha256(changed.encode()).hexdigest(),
            uv='0.9.5',model_requests=0),indent=2))
        # Harbor requires upload sources inside the task root. Give only this
        # verifier a shallow task view rooted at its private grading copy; the
        # original Task and its shared content digest remain untouched.
        self._original_task=self.task
        self.task=copy.copy(self.task)
        self.task._task_dir=Path(self._bootstrap_tmp.name)
        self.task.paths=TaskPaths(self.task._task_dir)
        return [target],target,target/script.relative_to(source)

    async def verify(self):
        from experiment_api.harbor_agent import RelayNativeAgent
        from experiment_api.process_cleanup import SCRIPT
        cid=await self.environment._platform._resolve_service_container('main')
        proof=await RelayNativeAgent._docker('exec',cid,'/opt/bench/python312/bin/python3.12','-c',SCRIPT)
        self.trial_paths.verifier_dir.mkdir(parents=True,exist_ok=True)
        (self.trial_paths.verifier_dir/'grader-implementation.json').write_text(json.dumps(dict(
            source_sha256=LOADED_SOURCE_SHA256,verifier_class=type(self).__name__,
            scope='Source bytes at module import; dispatch-time hash can differ for older in-flight revisions',
            model_calls=0),indent=2))
        (self.trial_paths.verifier_dir/'native-stopped-before-tests.json').write_text(proof)
        await self._preserve_agent_filesystem(cid)
        # Copy only dependency wheels; shared host cache is mounted read-only.
        result=await self.environment.exec(shlex.join(['/opt/bench/python312/bin/python3.12','-c',CACHE_COPY_SCRIPT]),timeout_sec=600)
        (self.trial_paths.verifier_dir/'cache-copy.json').write_text(json.dumps(dict(
            exit_code=result.return_code,stdout=result.stdout,stderr=result.stderr,
            script_sha256=hashlib.sha256(CACHE_COPY_SCRIPT.encode()).hexdigest(),model_calls=0),indent=2))
        if result.return_code:raise RuntimeError('Verifier dependency-cache copy failed')
        # Some official scripts install extra OS packages beyond curl. Keep
        # those commands unchanged, but use a reachable signed archive mirror.
        script=(self.task.paths.task_dir/'tests/test.sh').read_text()
        if re.search(r'^apt-get (update|install)',bootstrap_script(script),re.M):
            from .infra.apt_transport import SCRIPT as transport
            result=await self.environment.exec(shlex.join(['/opt/bench/python312/bin/python3.12','-c',transport]),timeout_sec=30)
            if result.return_code:raise RuntimeError('Grader APT mirror setup failed')
            (self.trial_paths.verifier_dir/'apt-transport.json').write_text(result.stdout or '{}')
            audit=await self.environment.exec('dpkg --audit',timeout_sec=30)
            if audit.return_code:raise RuntimeError('Could not inspect grading package state')
            if (audit.stdout or '').strip():
                configured=await self.environment.exec('DEBIAN_FRONTEND=noninteractive dpkg --configure -a',timeout_sec=600)
                fixed=None
                if configured.return_code:
                    fixed=await self.environment.exec('DEBIAN_FRONTEND=noninteractive apt-get -f install -y',timeout_sec=600)
                (self.trial_paths.verifier_dir/'dpkg-recovery.json').write_text(json.dumps(dict(
                    reason='Agent stopped during OS package installation; complete pending configuration before original grader dependencies',
                    audit=audit.stdout,configure_exit_code=configured.return_code,
                    fix_dependencies_exit_code=fixed.return_code if fixed else None,model_calls=0,
                    scope='Package configuration only; task output files and original test commands unchanged'),indent=2))
                if configured.return_code and (fixed is None or fixed.return_code):
                    raise RuntimeError('Pending package configuration failed before grading')
            # A missing grader executable must never masquerade as an Agent0.
            # Install exactly the script-declared packages and fail closed.
            from .infra.apt_archive import SCRIPT as ARCHIVE_SCRIPT
            commands=[shlex.split(line) for line in bootstrap_script(script).splitlines()
                if re.match(r'^apt-get (update|install)\b',line)]
            for argv in commands:
                if argv[:3]==['apt-get','install','-y']:
                    packages=argv[3:]
                    if not all(re.fullmatch(r'[A-Za-z0-9_+.=:-]+',p) for p in packages):
                        raise RuntimeError('Unrecognized declared APT package')
                    update=await self.environment.exec('apt-get update',timeout_sec=120)
                    if update.return_code:raise RuntimeError('Official grader APT update failed')
                    fetched=await self.environment.exec(shlex.join(['env','TB2_GRADER_APT_PACKAGES='+json.dumps(packages),
                        '/opt/bench/python312/bin/python3.12','-c',ARCHIVE_SCRIPT]),timeout_sec=240)
                    (self.trial_paths.verifier_dir/'apt-archive.json').write_text(fetched.stdout or '{}')
                    if fetched.return_code:raise RuntimeError('Official grader package archive checksum preflight failed')
                    installed=await self.environment.exec('DEBIAN_FRONTEND=noninteractive '+shlex.join(argv),timeout_sec=600)
                    (self.trial_paths.verifier_dir/'official-os-dependencies.json').write_text(json.dumps(dict(
                        command=argv,exit_code=installed.return_code,stdout=installed.stdout,stderr=installed.stderr,
                        model_calls=0,phase='Agent stopped; within original verifier budget'),indent=2))
                    if installed.return_code:raise RuntimeError('Declared grader OS packages not installed; do not interpret as Agent0')
        if self.task.paths.task_dir.name=='qemu-alpine-ssh':
            from .infra.prepare_os_tools import TARGET
            from .infra.common import HERE,digest,read_json
            pin=read_json(HERE/'manifests/qemu-grader-tool.json')
            if not TARGET.exists() or digest(TARGET)!=pin['binary_sha256']:
                raise RuntimeError('Prepare checksum-verified grader tool before running QEMU')
            await command('docker','cp',TARGET,cid+':/usr/local/bin/sshpass')
            checked=await self.environment.exec('sha256sum /usr/local/bin/sshpass',timeout_sec=30)
            if checked.return_code or checked.stdout.split()[0]!=pin['binary_sha256']:
                raise RuntimeError('Uploaded QEMU grader tool differs from pin')
            (self.trial_paths.verifier_dir/'grader-tool-provenance.json').write_text(json.dumps(dict(
                **pin,phase='After Agent stop, before hidden tests',installed_path='/usr/local/bin/sshpass',
                verified_container_sha256=checked.stdout.split()[0],task_services_untouched=True),indent=2))
        self.override_env.update(UV_PYTHON_INSTALL_DIR='/opt/bench/python-managed',
            UV_PYTHON_DOWNLOADS='never',UV_INDEX_URL=(self.verifier_env or {}).get('UV_INDEX_URL','https://pypi.org/simple'),
            PIP_INDEX_URL=(self.verifier_env or {}).get('UV_INDEX_URL','https://pypi.org/simple'),
            PATH='/opt/bench/bin/uv095:/opt/bench/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')
        indexes=grading_indexes(self.task.paths.task_dir.name)
        self.override_env.update(indexes)
        if self.task.paths.task_dir.name=='build-cython-ext':
            from .infra.prepare_git_inputs import verify,environment,CONTAINER_PATH
            _,mounts,_=await command('docker','inspect',cid,'--format','{{json .Mounts}}')
            mountlist=json.loads(mounts)
            source=next(m['Source'] for m in mountlist if m['Destination']=='/opt/bench/verifier-cache')
            cache=Path(source).parent/'grader-git/pyknotid.git';pin=verify(cache)
            mounted=next((m for m in mountlist if m['Destination']==CONTAINER_PATH),None)
            if mounted:
                # Only zero-model oracle controls mount the pinned public input.
                assert Path(mounted['Source']).resolve()==cache.resolve() and not mounted['RW']
            else:
                created=await self.environment.exec('mkdir -p '+CONTAINER_PATH,timeout_sec=30)
                assert created.return_code==0
                await command('docker','cp',str(cache)+'/.',cid+':'+CONTAINER_PATH,timeout=120)
            self.override_env.update(environment())
            (self.trial_paths.verifier_dir/'git-transport.json').write_text(json.dumps(dict(
                model_calls=0,phase='Agent stopped; grader only',
                transport='Pinned local bare Git input; official URL rewritten only inside grader',
                pin=pin,official_test_bytes_unchanged=True,agent_environment_unchanged=True),indent=2))
        (self.trial_paths.verifier_dir/'dependency-policy.json').write_text(json.dumps(dict(
            task=self.task.paths.task_dir.name,index_environment=indexes,
            phase='grader only; after Agent stopped',official_version_constraints_unchanged=True),indent=2))
        try:return await super().verify()
        finally:
            if hasattr(self,'_original_task'):self.task=self._original_task
            if hasattr(self,'_bootstrap_tmp'):self._bootstrap_tmp.cleanup()
