"""Run the frozen config with a single model gateway and fresh native sessions."""
import asyncio
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import time

from .common import ROOT, command, digest, jsonl, now, read_json, save_json
from .prepare import freeze_plan
from .scoring import harbor_grade


def source_hashes():
    return {str(p.relative_to(ROOT)): digest(p)
        for directory in ['harness_benchmark', 'experiment_api']
        for p in sorted((ROOT/directory).rglob('*')) if p.suffix in ['.py', '.cjs']}


def check_assets(out):
    for manifest, folder in [('assets.sha256.json', 'assets'), ('execution-assets.sha256.json', 'execution_assets')]:
        for name, sha in read_json(out/manifest).items():
            if digest(out/folder/name) != sha:
                raise ValueError('Prepared assets changed: '+name)


def pending_cells(plan, out):
    pending = []
    for cell in plan['cells']:
        folder = out/'cells'/f'{cell["index"]:05d}'
        if (folder/'record.json').exists():
            record = read_json(folder/'record.json')
            if any(record.get(key) != cell[key] for key in ['dataset', 'task', 'harness']):
                raise ValueError('Record identity differs from the frozen plan: '+str(folder))
            continue
        if (folder/'started.json').exists():
            raise ValueError('An unfinished started trial requires manual review: '+str(folder))
        pending.append(cell)
    return pending


def check_provider_evidence(record, events):
    invalid = any(event.get('status') in [401,402,403] or
        (event.get('response_models') and not event.get('model_echo_allowed')) for event in events)
    if invalid:
        record['observed_grader_score'] = record.get('official_score')
        record['official_score'] = None
        record['status'] = 'provider_error'
    return record


async def start_world(plan, cell, folder):
    cfg = plan['config']['execution']
    ds = next(ds for ds in plan['datasets'] if ds['name'] == 'appworld')
    worker = folder/'world'
    worker.mkdir(parents=True, exist_ok=True)
    output = (worker/'stdout.log').open('wb')
    error = (worker/'stderr.log').open('wb')
    name = 'hb_'+hashlib.sha256((plan['plan_sha256']+str(cell['index'])).encode()).hexdigest()[:20]
    process = await asyncio.create_subprocess_exec(cfg['appworld_python'], ROOT/'harness_benchmark/appworld_worker.py',
        '--task', cell['task'], '--output', worker, '--root', cfg['appworld_root'], '--experiment', name,
        '--seed', str(ds['seed']), '--max-interactions', str(cfg['appworld_max_interactions']),
        '--code-timeout', str(cfg['appworld_code_timeout_sec']), stdout=output, stderr=error)
    output.close()
    error.close()
    try:
        for _ in range(600):
            if process.returncode is not None:
                raise RuntimeError('AppWorld worker failed; see private world/stderr.log')
            if (worker/'ready.json').exists():
                return process, worker/'ready.json'
            await asyncio.sleep(.2)
        raise TimeoutError('AppWorld initialization timed out')
    except BaseException:
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise


def world_task(folder, ready, spec, cfg):
    task = folder/'appworld-task'
    (task/'environment').mkdir(parents=True, exist_ok=True)
    (task/'tests').mkdir(exist_ok=True)
    (task/'instruction.md').write_text(read_json(ready)['prompt'])
    (task/'task.toml').write_text(f'''version = "1.0"
[agent]
timeout_sec = {cfg['appworld_timeout_sec']}
[verifier]
timeout_sec = 180
[environment]
docker_image = "{spec['image_id']}"
cpus = {cfg['minimum_cpus']}
memory_mb = {cfg['minimum_memory_mb']}
''')
    (task/'environment/Dockerfile').write_text('FROM '+spec['image_id']+'\nWORKDIR /app\n')
    (task/'tests/test.sh').write_text('# AppWorld is evaluated by the host AppWorldVerifier.\nexit 1\n')
    save_json(task/'appworld-runtime.json', {'ready_file': str(ready)})
    return task


def extract_result(out, cell, folder, trialfile):
    trial = read_json(trialfile)
    record = dict(cell, provider='relay', official_score=None, status='unscored',
        result_path=str(trialfile.relative_to(out)), agent_started=bool((trial.get('agent_execution') or {}).get('started_at')),
        exception=(trial.get('exception_info') or {}).get('exception_type'))
    for key, field in [('prepare_sec', 'environment_setup'), ('agent_sec', 'agent_execution'), ('verifier_sec', 'verifier')]:
        from datetime import datetime
        timing = trial.get(field) or {}
        a, b = timing.get('started_at'), timing.get('finished_at')
        record[key] = (datetime.fromisoformat(b)-datetime.fromisoformat(a)).total_seconds() if a and b else None
    verifier = trialfile.parent/'verifier'
    reward = ((trial.get('verifier_result') or {}).get('rewards') or {}).get('reward')
    if cell['dataset'] == 'appworld':
        from .appworld_adapter import score_evaluation
        evidence = verifier/'evaluation.json'
        if evidence.exists():
            score = score_evaluation(read_json(evidence))
            if reward == score:
                record['official_score'] = score
    else:
        path = verifier/('report.json' if cell['dataset'] == 'swebench-verified' else 'ctrf.json')
        evidence = read_json(path) if path.exists() else {}
        if cell['dataset'] == 'swebench-verified':
            evidence = evidence.get(cell['task'], {})
        stdout = (verifier/'test-stdout.txt').read_text(errors='replace') if (verifier/'test-stdout.txt').exists() else ''
        record['official_score'] = harbor_grade(cell['dataset'], reward, evidence, stdout)
    identity = trialfile.parent/'agent/benchmark-identity.json'
    if identity.exists():
        info = read_json(identity)
        record.update(trial=info['trial'], configuration_sha256=info['api_configuration_sha256'])
    resources = trialfile.parent/'agent/resource-summary.json'
    if resources.exists():
        record['resources'] = read_json(resources)
    record['agent_timed_out'] = record['exception'] == 'AgentTimeoutError'
    if record['official_score'] is not None:
        record['status'] = 'official_scored'
    return record


async def cleanup(folder, scored):
    identities = list((folder/'jobs').glob('*/*/agent/container-identity.json'))
    if not identities:
        return
    cid = read_json(identities[0])['container_id']
    rc, raw, _ = await command('docker', 'inspect', cid, check=False)
    if rc:
        return
    info = json.loads(raw)[0]
    project = info.get('Config', {}).get('Labels', {}).get('com.docker.compose.project')
    if not project or not project.endswith('__env'):
        raise RuntimeError('Cannot prove container ownership; refusing cleanup')
    if info['State'].get('Running'):
        from experiment_api.process_cleanup import SCRIPT
        _, proof, _ = await command('docker','exec',cid,'/opt/bench/python312/bin/python3.12','-c',SCRIPT,timeout=45)
        (folder/'post-run-agent-stopped.json').write_text(proof)
    save_json(folder/'environment.json', dict(container_id=cid, project=project, image_id=info['Image'],
        retained_for_grading_review=not scored, state=info['State']))
    if not scored:
        return
    _, raw, _ = await command('docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project='+project)
    if raw.split():
        await command('docker', 'rm', '-f', *raw.split())
    _, raw, _ = await command('docker', 'network', 'ls', '-q', '--filter', 'label=com.docker.compose.project='+project)
    for network in raw.split():
        await command('docker', 'network', 'rm', network)


async def run(plan):
    from aiohttp import web
    from experiment_api.config import load_settings, public_fingerprint
    from experiment_api.gateway import PRIVATE, create_app
    from experiment_api.runtime import prepare_runtime
    from experiment_api.support import VERSIONS
    out = freeze_plan(plan)
    lock = (out/'run.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.environ['HARNESS_BENCH_API_CONFIG'] = plan['config']['api_config']
    settings = load_settings()
    cfg = plan['config']['execution']
    check_assets(out)
    images = read_json(out/'images.json')
    contract = dict(plan_sha256=plan['plan_sha256'], api=settings.public(),
        configuration_sha256=public_fingerprint(settings), harness_versions={h: VERSIONS[h] for h in plan['harnesses']},
        source_sha256=source_hashes(), images_sha256=digest(out/'images.json'), repetitions=1,
        policy='First execution only; retain valid0; never automatically retry an Agent')
    if (out/'execution.json').exists() and read_json(out/'execution.json') != contract:
        raise ValueError('Frozen execution/model/code changed; choose a new output_dir')
    save_json(out/'execution.json', contract)
    _, overlay = prepare_runtime(settings)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)
    master = PRIVATE/'master.txt'
    if not master.exists():
        master.write_text(secrets.token_hex(32))
        master.chmod(0o600)
    socket = PRIVATE/'socket/gateway.sock'
    if socket.exists():
        raise ValueError('A gateway already exists; inspect the active run')
    pending = pending_cells(plan, out)
    if not pending:
        print('All configured cells already have records; no Agent was rerun')
        return
    consumed = sum(len(jsonl(p)) for p in out.glob('requests.*.jsonl'))
    remaining = cfg['max_requests']-consumed
    if remaining <= 0:
        raise ValueError('The configured request limit is exhausted')
    segment = len(list(out.glob('requests.*.jsonl')))+1
    app = create_app(settings, master.read_text(), out/f'requests.{segment:03d}.jsonl', remaining, PRIVATE/'routes')
    runner = web.AppRunner(app)
    stop = asyncio.Event()
    active = {}
    queue = asyncio.Queue()
    for cell in pending:
        queue.put_nowait(cell)
    for sig in [signal.SIGTERM, signal.SIGINT]:
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    def status():
        records = [read_json(p) for p in (out/'cells').glob('*/record.json')]
        save_json(out/'status.json', dict(updated_at=now(), planned=len(plan['cells']), completed=len(records),
            valid_scored=sum(r.get('official_score') is not None for r in records), active=active, stopping=stop.is_set()))

    async def worker(slot):
        while not queue.empty() and not stop.is_set():
            state = app['benchmark_state']
            if state['identity_frozen'] or state['access_blocked'] or state['dispatched'] >= remaining:
                stop.set()
                break
            cell = queue.get_nowait()
            folder = out/'cells'/f'{cell["index"]:05d}'
            folder.mkdir(parents=True, exist_ok=True)
            save_json(folder/'started.json', dict(**cell, started_at=now(), model=settings.model,
                configuration_sha256=public_fingerprint(settings)))
            active[str(slot)] = cell
            status()
            began = time.monotonic()
            record = dict(cell, status='infrastructure_error', official_score=None, agent_started=False,
                model=settings.model, configuration_sha256=public_fingerprint(settings))
            world = monitor = future = None
            try:
                local = copy.deepcopy(overlay)
                environment = {'PIP_INDEX_URL': cfg['pypi_index'], 'UV_INDEX_URL': cfg['pypi_index']}
                if cfg['hf_endpoint']:
                    environment['HF_ENDPOINT'] = cfg['hf_endpoint']
                local['services']['main'].update(pull_policy='never', environment=environment)
                agent = 'experiment_api.harbor_agent:RelayNativeAgent'
                extra_agent = []
                if cell['dataset'] == 'appworld':
                    world, ready = await start_world(plan, cell, folder)
                    from experiment_api.resource_monitor import ResourceMonitor
                    monitor = ResourceMonitor(folder/'world-resources.jsonl', lambda: [world.pid])
                    future = asyncio.create_task(monitor.run())
                    spec = {**images['appworld'], 'cpus': cfg['minimum_cpus'], 'memory_mb': cfg['minimum_memory_mb'],
                        'agent_timeout_sec': cfg['appworld_timeout_sec'], 'verifier_timeout_sec': 180}
                    task = world_task(folder, ready, spec, cfg)
                    agent = 'harness_benchmark.appworld_adapter:AppWorldRelayAgent'
                    verifier = 'harness_benchmark.appworld_adapter:AppWorldVerifier'
                    extra_agent = ['--ak', 'appworld_ready_file='+str(ready)]
                    local['services']['main']['working_dir'] = '/app'
                    local['services']['main']['volumes'].append(dict(type='bind', source=str(ROOT/'harness_benchmark/appworld_command.py'),
                        target='/opt/bench/bin/appworld', read_only=True))
                else:
                    key = cell['dataset']+'/'+cell['task']
                    spec = images[key]
                    task = out/'execution_assets'/key
                    verifier = 'harness_benchmark.verifier:TerminalVerifier' if cell['dataset'] == 'terminal-bench' else 'harness_benchmark.swe_verifier:SWEVerifier'
                    local['services']['main']['volumes'].extend([
                        dict(type='bind', source=str(out/'dependencies/uv-cache'), target='/opt/bench/verifier-cache', read_only=True),
                        dict(type='bind', source=str(out/'dependencies/python-managed'), target='/opt/bench/python-managed', read_only=True)])
                    if cell['dataset'] == 'swebench-verified':
                        local['services']['main']['working_dir'] = '/testbed'
                        local['services']['main']['volumes'].append(dict(type='bind',
                            source=str(out/'dependencies/swe-parser/lib/python3.12/site-packages'), target='/opt/bench/swe-parser', read_only=True))
                compose = folder/'overlay.json'
                save_json(compose, local)
                args = [ROOT/'.venv-harbor/bin/harbor', 'run', '-p', task, '-a', agent,
                    '--ak', 'harness='+cell['harness'], '--ak', 'agent_network='+cfg['agent_network'], *extra_agent,
                    '-m', settings.model, '-e', 'docker', '--ek', 'extra_docker_compose='+json.dumps([str(compose)]),
                    '--ek', 'keep_containers=true', '-k', '1', '-n', '1', '--max-retries', '0',
                    '--job-name', 'hb-'+plan['plan_sha256'][:8]+'-'+str(cell['index']), '-o', folder/'jobs',
                    '--verifier', verifier, '--ve', 'UV_INDEX_URL='+cfg['pypi_index'],
                    '--override-cpus', str(spec['cpus']), '--override-memory-mb', str(spec['memory_mb'])]
                save_json(folder/'command.json', list(map(str, args)))
                rc, _, _ = await command(*args, timeout=spec['agent_timeout_sec']+spec['verifier_timeout_sec']+900,
                    check=False, output=folder/'harbor.log', env={**os.environ, 'PYTHONPATH': str(ROOT)})
                trials = [p for p in (folder/'jobs').glob('*/*/result.json') if 'task_name' in read_json(p)]
                if len(trials) != 1:
                    raise ValueError('Expected one Harbor trial result')
                record = {**record, **extract_result(out, cell, folder, trials[0]), 'harbor_exit_code': rc}
                events = [event for file in out.glob('requests.*.jsonl') for event in jsonl(file)
                    if event.get('trial') == record.get('trial')]
                check_provider_evidence(record, events)
                if record['official_score'] is None:
                    stop.set()
            except Exception as error:
                record.update(error_type=type(error).__name__, error=str(error)[-1200:])
                stop.set()
            finally:
                if monitor:
                    monitor.stopped = True
                    await future
                    from experiment_api.resource_monitor import resource_summary
                    record['world_worker_resources'] = resource_summary(folder/'world-resources.jsonl')
                if world and world.returncode is None:
                    world.terminate()
                    try:
                        await asyncio.wait_for(world.wait(), 15)
                    except asyncio.TimeoutError:
                        world.kill()
                        await world.wait()
                record.update(finished_at=now(), total_sec=time.monotonic()-began)
                save_json(folder/'record.json', record)
                try:
                    await cleanup(folder, record.get('official_score') is not None)
                except Exception as error:
                    save_json(folder/'cleanup-error.json', {'error_type': type(error).__name__})
                    stop.set()
                active.pop(str(slot), None)
                status()
                queue.task_done()
                print(json.dumps({key: record.get(key) for key in ['dataset', 'task', 'harness', 'status', 'official_score']}, ensure_ascii=False), flush=True)
    await runner.setup()
    try:
        socket.parent.mkdir(parents=True, exist_ok=True)
        await web.UnixSite(runner, str(socket)).start()
        socket.chmod(0o666)
        await asyncio.gather(*(worker(i) for i in range(cfg['workers'])))
    finally:
        await runner.cleanup()
        socket.unlink(missing_ok=True)
        status()
        save_json(out/f'closed.{segment:03d}.json', {'closed_at': now(), 'pending': queue.qsize()})
