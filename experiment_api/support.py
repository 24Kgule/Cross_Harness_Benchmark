"""Frozen CLI versions, setup and mounts; no historical provider run path."""
from pathlib import Path
from harbor.agents.base import BaseAgent
ROOT=Path(__file__).resolve().parents[1]
PRIVATE=ROOT/'experiment_api/private'
VERSIONS={'codex':'0.163.0','claude':'2.1.310','dsh':'0.2.0-rc.2','opencode':'1.19.4','pi':'1.4.0','kimi':'2.3.1'}

class FrozenNativeBase(BaseAgent):
    def __init__(self, *args, harness='dsh', **kwargs):
        if harness not in VERSIONS:
            raise ValueError('Unknown frozen harness')
        self.harness = harness
        super().__init__(*args, **kwargs)

    @staticmethod
    def name():
        return 'single-model-native-harness'

    def version(self):
        return VERSIONS[self.harness]

    async def setup(self, environment):
        # Runtime mounts are read-only; no CLI installation or updates in a task.
        result = await environment.exec(
            '/opt/bench/python312/bin/python3.12 --version && /opt/bench/bin/node --version',
            timeout_sec=30)
        if result.return_code:
            raise RuntimeError('Frozen runtime mounts unavailable')

def runtime_overlay():
    """Mount only frozen runtimes, the new socket, and public model metadata."""
    mounts = [
        (ROOT / 'vendor/npm/node_modules', '/opt/bench/npm/node_modules'),
        (ROOT / 'vendor/dsh/node_modules', '/opt/bench/dsh/node_modules'),
        (ROOT / '.runtime/bin', '/opt/bench/bin'),
        (ROOT / '.runtime/python/cpython-3.12.15-linux-x86_64-gnu', '/opt/bench/python312'),
        (ROOT / 'harness_benchmark/native_launcher.py', '/opt/bench/native_entry.py'),
        (ROOT / 'harness_benchmark/relay.cjs', '/opt/bench/relay.cjs'),
        (ROOT / 'configs/codex.models.template.json', '/opt/bench/codex.models.json'),
        (PRIVATE / 'socket', '/run/bench-gateway'),
    ]
    for source, _ in mounts:
        if not source.exists():
            raise FileNotFoundError(source)
    return {'services': {'main': {'volumes': [
        {'type': 'bind', 'source': str(source), 'target': target, 'read_only': True}
        for source, target in mounts
    ]}}}
