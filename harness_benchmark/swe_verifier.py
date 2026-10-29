"""Use official SWE grading functions with local, pinned parser dependencies."""
import difflib
import copy
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile

from harbor.verifier.verifier import Verifier
from harbor.models.task.paths import TaskPaths


def prepare_script(source, task_name):
    # make_test_spec also builds shell setup by downloading repository metadata.
    # Parsing logs needs only these five dataset fields, with identical values.
    replacement = '''from types import SimpleNamespace
test_spec = SimpleNamespace(repo=datum["repo"], version=datum["version"],
    instance_id=datum["instance_id"],
    FAIL_TO_PASS=json.loads(datum["FAIL_TO_PASS"]) if isinstance(datum["FAIL_TO_PASS"], str) else datum["FAIL_TO_PASS"],
    PASS_TO_PASS=json.loads(datum["PASS_TO_PASS"]) if isinstance(datum["PASS_TO_PASS"], str) else datum["PASS_TO_PASS"])
'''.rstrip()
    changed, count = re.subn(r'(?m)^test_spec\s*=\s*make_test_spec\(datum\)$', replacement, source)
    if count != 1 or source.count('uv run parser.py |') != 1:
        raise ValueError('Unrecognized SWE parser bootstrap')
    changed = changed.replace('uv run parser.py |',
        'PYTHONPATH=/opt/bench/swe-parser /opt/bench/python312/bin/python3.12 parser.py |')
    if task_name.startswith('sphinx-doc__'):
        old = 'tox --current-env -epy39 -v -- '
        if old not in changed:
            raise ValueError('Unrecognized Sphinx invocation; review required')
        changed = changed.replace(old, old+'-rA --color=no ')
    return changed


class SWEVerifier(Verifier):
    def _resolve_tests(self):
        dirs, source, script = super()._resolve_tests()
        if dirs != [source]:
            raise ValueError('Expected one SWE test directory')
        logs = self.trial_paths.verifier_dir
        logs.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix='swe-grader-', dir=logs)
        target = Path(self._temporary.name)/'tests'
        shutil.copytree(source, target)
        original = script.read_text()
        changed = prepare_script(original, self.task.paths.task_dir.name)
        prepared = target/script.relative_to(source)
        prepared.write_text(changed)
        hashes = {str(p.relative_to(source)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.rglob('*') if p.is_file() and p != script}
        if any(hashlib.sha256((target/name).read_bytes()).hexdigest() != sha for name, sha in hashes.items()):
            raise ValueError('SWE grading assets changed')
        (logs/'bootstrap.diff').write_text(''.join(difflib.unified_diff(original.splitlines(True), changed.splitlines(True))))
        (logs/'bootstrap.json').write_text(json.dumps(dict(test_files_sha256=hashes,
            original_script_sha256=hashlib.sha256(original.encode()).hexdigest(),
            prepared_script_sha256=hashlib.sha256(changed.encode()).hexdigest(),
            parser='swebench4.0.3 official grading functions', model_calls=0), indent=2))
        self._original_task = self.task
        self.task = copy.copy(self.task)
        self.task._task_dir = Path(self._temporary.name)
        self.task.paths = TaskPaths(self.task._task_dir)
        return [target], target, prepared

    async def verify(self):
        from experiment_api.harbor_agent import RelayNativeAgent
        from experiment_api.process_cleanup import SCRIPT
        cid = await self.environment._platform._resolve_service_container('main')
        logs = self.trial_paths.verifier_dir
        logs.mkdir(parents=True, exist_ok=True)
        proof = await RelayNativeAgent._docker('exec', cid, '/opt/bench/python312/bin/python3.12', '-c', SCRIPT)
        (logs/'native-stopped-before-tests.json').write_text(proof)
        self.override_env.update(PATH='/opt/bench/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
            UV_PYTHON='/opt/bench/python312/bin/python3.12', UV_PYTHON_DOWNLOADS='never')
        try:
            return await super().verify()
        finally:
            if hasattr(self, '_original_task'):
                self.task = self._original_task
            if hasattr(self, '_temporary'):
                self._temporary.cleanup()
