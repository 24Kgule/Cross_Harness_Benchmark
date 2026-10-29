"""Initialize a fresh official AppWorld, exercise its bridge and grade a NOP.

No model calls. Requires AppWorld installed and its dataset downloaded.
This validates plumbing; the resulting evaluation is not a harness score.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import aiohttp


async def check(args):
    experiment = 'smoke_'+uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as directory:
        out = Path(directory)
        error = (out/'stderr.log').open('wb')
        process = await asyncio.create_subprocess_exec(args.python,ROOT/'harness_benchmark/appworld_worker.py',
            '--task',args.task,'--output',out,'--root',args.root,'--experiment',experiment,
            '--seed','1','--max-interactions','10','--code-timeout','30',stderr=error)
        try:
            for _ in range(600):
                if (out/'ready.json').exists():
                    break
                if process.returncode is not None:
                    raise RuntimeError((out/'stderr.log').read_text()[-1500:])
                await asyncio.sleep(.2)
            else:
                raise TimeoutError('AppWorld worker not ready')
            ready = json.loads((out/'ready.json').read_text())
            headers = {'Authorization':'Bearer '+ready['token']}
            async with aiohttp.ClientSession() as client:
                async with client.post(ready['bridge']+'/execute',json={'code':'print(1)'}) as response:
                    assert response.status == 401
                for code in ['smoke_variable = 42', 'print(smoke_variable)']:
                    async with client.post(ready['bridge']+'/execute',json={'code':code},headers=headers) as response:
                        response.raise_for_status()
                        body = await response.json()
                assert '42' in body['output']
                async with client.post(ready['bridge']+'/evaluate',json={},headers=headers) as response:
                    response.raise_for_status()
                    evaluation = await response.json()
                from harness_benchmark.appworld_adapter import score_evaluation
                assert score_evaluation(evaluation) == 0
            print('AppWorld initialization, persistent execution, authentication and official NOP evaluation: OK; model calls=0')
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(),15)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            error.close()
            shutil.rmtree(Path(args.root)/'experiments/outputs'/experiment,ignore_errors=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python',default=str(ROOT/'.venv-appworld/bin/python'))
    parser.add_argument('--root',default=str(ROOT/'datasets/appworld'))
    parser.add_argument('--task',default='024c982_1')
    asyncio.run(check(parser.parse_args()))
