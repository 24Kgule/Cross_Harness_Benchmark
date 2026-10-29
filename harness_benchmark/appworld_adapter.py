"""Connect the native Agent lifecycle to its private AppWorld worker."""
import json
from pathlib import Path

from experiment_api.gateway import PRIVATE
from experiment_api.harbor_agent import RelayNativeAgent
from harbor.models.verifier.result import VerifierResult
from harbor.verifier.verifier import Verifier


class AppWorldRelayAgent(RelayNativeAgent):
    def __init__(self, *args, appworld_ready_file, **kwargs):
        self.ready = Path(appworld_ready_file)
        super().__init__(*args, **kwargs)

    async def run(self, instruction, environment, context):
        trial = 'relay_'+str(self.context_id or self.session_id)+'_'+self.harness
        routes = PRIVATE/'routes'
        routes.mkdir(parents=True, exist_ok=True)
        route = routes/(trial+'.json')
        info = json.loads(self.ready.read_text())
        route.write_text(json.dumps({key: info[key] for key in ['bridge', 'token']}))
        route.chmod(0o600)
        try:
            return await super().run(instruction, environment, context)
        finally:
            route.unlink(missing_ok=True)


def score_evaluation(evaluation):
    if isinstance(evaluation.get('success'), bool):
        return float(evaluation['success'])
    if isinstance(evaluation.get('passes'), list) and isinstance(evaluation.get('failures'), list):
        if not evaluation['passes'] and not evaluation['failures']:
            raise ValueError('AppWorld evaluation contains no tests')
        return float(not evaluation['failures'])
    raise ValueError('Unrecognized AppWorld official evaluation')


class AppWorldVerifier(Verifier):
    async def verify(self):
        import aiohttp
        from experiment_api.process_cleanup import SCRIPT
        cid = await self.environment._platform._resolve_service_container('main')
        proof = await RelayNativeAgent._docker('exec', cid, '/opt/bench/python312/bin/python3.12', '-c', SCRIPT)
        logs = self.trial_paths.verifier_dir
        logs.mkdir(parents=True, exist_ok=True)
        (logs/'native-stopped-before-tests.json').write_text(proof)
        runtime = json.loads((self.task.paths.task_dir/'appworld-runtime.json').read_text())
        info = json.loads(Path(runtime['ready_file']).read_text())
        async with aiohttp.ClientSession() as client:
            async with client.post(info['bridge']+'/evaluate', json={},
                headers={'Authorization': 'Bearer '+info['token']},
                timeout=aiohttp.ClientTimeout(total=150)) as response:
                response.raise_for_status()
                evaluation = await response.json()
        score = score_evaluation(evaluation)
        (logs/'evaluation.json').write_text(json.dumps(evaluation, indent=2))
        (logs/'reward.txt').write_text(str(score)+'\n')
        return VerifierResult(rewards={'reward': score})
