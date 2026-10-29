"""Exercise Harbor + the container verifier with a synthetic task and NOP Agent.

No gateway, credential or model is used. Requires the installed private runtime
and a local python:3.12-slim image (docker pull it first if missing).
"""
import asyncio
import json
from pathlib import Path
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from experiment_api.support import runtime_overlay
from harness_benchmark.common import command, save_json
from harness_benchmark.scoring import harbor_grade


async def main():
    with tempfile.TemporaryDirectory(prefix='harness-nop-') as directory:
        out = Path(directory)
        task = out/'synthetic-plumbing-check'
        (task/'environment').mkdir(parents=True)
        (task/'tests').mkdir()
        (task/'instruction.md').write_text('No operation; plumbing check only.\n')
        _, image, _ = await command('docker','image','inspect','python:3.12-slim','--format','{{.Id}}')
        image = image.strip()
        (task/'environment/Dockerfile').write_text('FROM '+image+'\n')
        (task/'task.toml').write_text(f'''version = "1.0"
[agent]
timeout_sec = 30
[verifier]
timeout_sec = 120
[environment]
docker_image = "{image}"
cpus = 1
memory_mb = 1024
''')
        ctrf = {'results':{'tool':{'name':'synthetic-smoke'}, 'summary':{'tests':1,'passed':1,'failed':0,'skipped':0,'pending':0,'other':0,'start':0,'stop':1},
                           'tests':[{'name':'plumbing-only','status':'passed','duration':0}]}}
        (task/'tests/test.sh').write_text('#!/bin/bash\nmkdir -p /logs/verifier\ncat > /logs/verifier/ctrf.json <<\'JSON\'\n'+
            json.dumps(ctrf)+'\nJSON\necho 1 > /logs/verifier/reward.txt\n')
        overlay = runtime_overlay()
        (out/'cache/archive-v0').mkdir(parents=True)
        (out/'python').mkdir()
        overlay['services']['main']['pull_policy'] = 'never'
        overlay['services']['main']['volumes'].extend([
            dict(type='bind',source=str(out/'cache'),target='/opt/bench/verifier-cache',read_only=True),
            dict(type='bind',source=str(out/'python'),target='/opt/bench/python-managed',read_only=True)])
        save_json(out/'overlay.json',overlay)
        rc, _, _ = await command(ROOT/'.venv-harbor/bin/harbor','run','-p',task,'-a','nop','-e','docker',
            '--ek','extra_docker_compose='+json.dumps([str(out/'overlay.json')]),'--max-retries','0',
            '--job-name','local-smoke','-o',out/'jobs','--verifier','harness_benchmark.verifier:TerminalVerifier',
            check=False,timeout=240,output=out/'harbor.log',env={**os.environ,'PYTHONPATH':str(ROOT)})
        trials = [p for p in (out/'jobs').glob('*/*/result.json') if 'task_name' in json.loads(p.read_text())]
        if rc or len(trials)!=1:
            raise RuntimeError('NOP plumbing failed: '+(out/'harbor.log').read_text()[-2000:])
        trial = json.loads(trials[0].read_text())
        reward = ((trial.get('verifier_result') or {}).get('rewards') or {}).get('reward')
        if not (trials[0].parent/'verifier/ctrf.json').exists():
            details = json.dumps(trial.get('exception_info'))
            details += '\n'+(out/'harbor.log').read_text()[-3000:]
            raise RuntimeError('Verifier did not produce evidence: '+details)
        evidence = json.loads((trials[0].parent/'verifier/ctrf.json').read_text())
        assert harbor_grade('terminal-bench',reward,evidence)==1
        print('Docker + Harbor + native process cleanup + verifier + score extraction: OK; synthetic NOP only; model calls=0')


if __name__ == '__main__':
    asyncio.run(main())
