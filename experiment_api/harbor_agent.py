"""Harbor entry for all six native harnesses using the active relay gateway."""
import asyncio
import hashlib
import hmac
import json
import shlex
import time

from .support import FrozenNativeBase
from .config import load_settings, public_fingerprint
from .gateway import PRIVATE


class RelayNativeAgent(FrozenNativeBase):
    def __init__(self, *args, agent_network='task-default', **kwargs):
        if agent_network not in ['task-default', 'isolated']:
            raise ValueError('agent_network must be task-default or isolated')
        self.agent_network = agent_network
        super().__init__(*args, **kwargs)

    @staticmethod
    def name():
        return 'active-relay-native-benchmark'

    @staticmethod
    async def _docker(*arguments):
        process = await asyncio.create_subprocess_exec(
            'docker', *arguments, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.communicate()
            raise
        if process.returncode:
            raise RuntimeError('Task network operation failed: ' + stderr.decode(errors='replace')[-800:])
        return stdout.decode()

    async def _isolate_network(self, cid, environment, logs):
        mode = (await self._docker('inspect', cid, '--format', '{{.HostConfig.NetworkMode}}')).strip()
        if mode == 'host' or mode.startswith('container:'):
            raise ValueError('Isolated Agent execution requires an independent Docker network namespace')
        networks = json.loads(await self._docker('inspect', cid, '--format', '{{json .NetworkSettings.Networks}}'))
        (logs / 'network-before-agent.json').write_text(json.dumps(networks, indent=2))
        try:
            for name in networks:
                await self._docker('network', 'disconnect', name, cid)
            proof = await environment.exec(
                "/opt/bench/python312/bin/python3.12 -c "
                "'import json,socket; s=socket.socket(); s.settimeout(2); "
                "r=s.connect_ex((\"1.1.1.1\",443)); "
                "print(json.dumps({\"interfaces\":socket.if_nameindex(),\"external_connect_errno\":r})); "
                "assert r!=0'", timeout_sec=15)
            (logs / 'network-check.txt').write_text(proof.stdout or '')
            if proof.return_code:
                raise RuntimeError('Agent network isolation check failed before model execution')
            return networks
        except BaseException:
            await self._restore_network(cid, networks)
            raise

    async def _restore_network(self, cid, networks):
        current = json.loads(await self._docker('inspect', cid, '--format', '{{json .NetworkSettings.Networks}}'))
        for name, info in networks.items():
            if name in current:
                continue
            command = ['network', 'connect']
            for alias in info.get('Aliases') or []:
                command += ['--alias', alias]
            await self._docker(*command, name, cid)

    async def run(self, instruction, environment, context):
        from .monitoring import monitor_container, resource_summary

        settings = load_settings()
        if self.model_name != settings.model:
            raise ValueError('Harbor model must equal the model configured in the active .env')
        logs = self.logs_dir.resolve()
        logs.mkdir(parents=True, exist_ok=True)
        trial = 'relay_' + str(self.context_id or self.session_id) + '_' + self.harness
        token = hmac.new((PRIVATE / 'master.txt').read_bytes(), trial.encode(), 'sha256').hexdigest()
        prompt = instruction + (
            '\nBenchmark contract: solve autonomously with your native local tools. '
            'Do not spawn subagents, call another model, inspect benchmark reference '
            'solutions or hidden tests, or modify the benchmark runner. '
            + ('External network is unavailable during your work; use local tools. '
             if self.agent_network == 'isolated' else 'Package and task-data downloads are allowed. ')
            + 'Finish when done.\n')
        (logs / 'prompt.txt').write_text(prompt)
        (logs / 'benchmark-identity.json').write_text(json.dumps({
            **settings.public(), 'api_configuration_sha256': public_fingerprint(settings),
            'trial': trial, 'harness': self.harness, 'version': self.version(),
            'started_at': time.time(), 'gateway_scope': 'active_relay', 'separate_grading': True,
            'agent_network': self.agent_network,
        }, indent=2))
        cid = await environment._platform._resolve_service_container('main')
        (logs / 'container-identity.json').write_text(json.dumps({
            'container_id': cid, 'trial': trial,
        }, indent=2))
        networks = None
        isolated_at = time.monotonic()
        if self.agent_network == 'isolated':
            networks = await self._isolate_network(cid, environment, logs)
        isolation_sec = time.monotonic() - isolated_at
        monitor, future = await monitor_container(cid, logs / 'resources.jsonl')
        started = time.monotonic()
        try:
            result = await environment.exec(shlex.join([
                '/opt/bench/python312/bin/python3.12', '/opt/bench/native_entry.py',
                '--harness', self.harness, '--trial', trial, '--token', token]))
            (logs / 'launcher.stdout').write_text(result.stdout or '')
            (logs / 'launcher.stderr').write_text(result.stderr or '')
            (logs / 'native-exit-code.txt').write_text(str(result.return_code))
        finally:
            try:
                try:
                    from .process_cleanup import SCRIPT
                    proof = await self._docker('exec', cid, '/opt/bench/python312/bin/python3.12', '-c', SCRIPT)
                    (logs / 'native-terminated-before-grading.json').write_text(proof)
                finally:
                    monitor.stopped = True
                    await future
                    (logs / 'resource-summary.json').write_text(json.dumps({
                        'agent_sec': time.monotonic() - started,
                        'resources': resource_summary(logs / 'resources.jsonl'),
                        'network_isolation_sec': isolation_sec,
                    }, indent=2))
            finally:
                if networks is not None:
                    restored_at = time.monotonic()
                    await self._restore_network(cid, networks)
                    (logs / 'network-restored-for-verifier.json').write_text(json.dumps({
                        'restored': True, 'network_names': list(networks),
                        'restore_sec': time.monotonic() - restored_at,
                    }, indent=2))
