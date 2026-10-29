"""Download the configured sample, pin images, and prepare grading dependencies."""
import asyncio
import concurrent.futures
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import sys
import urllib.request

from .common import ROOT, command, digest, read_json, save_json


def freeze_plan(plan):
    out = Path(plan['config']['output_dir'])
    out.mkdir(parents=True, exist_ok=True)
    filename = out / 'plan.json'
    if filename.exists() and read_json(filename) != plan:
        raise ValueError('This output directory belongs to a different config/sample; choose a new output_dir')
    save_json(filename, plan)
    return out


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'cross-harness-benchmark'})
    with urllib.request.urlopen(request, timeout=90) as response:
        return response.read()


def git_blob(data):
    return hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()


def download_tasks(plan):
    out = freeze_plan(plan)
    groups = {}
    hashes = {}
    for ds in plan['datasets']:
        if ds['name'] == 'appworld':
            continue
        for task in ds['tasks']:
            groups.setdefault((task['git_url'], task['git_commit_id']), []).append((ds['name'], task))
    for (url, commit), tasks in groups.items():
        repository = url.removeprefix('https://github.com/').removesuffix('.git')
        treefile = out / 'downloads' / (commit+'.tree.json')
        if treefile.exists():
            tree = read_json(treefile)
        else:
            tree = json.loads(fetch(f'https://api.github.com/repos/{repository}/git/trees/{commit}?recursive=1'))
            save_json(treefile, tree)
        if tree.get('truncated') or 'tree' not in tree:
            raise RuntimeError('The pinned Git tree is incomplete')
        files = []
        for dsname, task in tasks:
            prefix = task['path'].rstrip('/')+'/'
            entries = [e for e in tree['tree'] if e['path'].startswith(prefix) and e['type'] == 'blob']
            if not entries:
                raise RuntimeError('No files for task '+task['name'])
            for entry in entries:
                if entry['mode'] not in ['100644', '100755']:
                    raise ValueError('Only ordinary task files are supported')
                relative = Path(entry['path'][len(prefix):])
                if relative.is_absolute() or '..' in relative.parts:
                    raise ValueError('Unsafe dataset path')
                target = out / 'assets' / dsname / task['name'] / relative
                files.append((entry, target))

        def download(item):
            entry, target = item
            data = target.read_bytes() if target.exists() else None
            if data is None or git_blob(data) != entry['sha']:
                data = fetch(f'https://raw.githubusercontent.com/{repository}/{commit}/{entry["path"]}')
                if git_blob(data) != entry['sha']:
                    raise RuntimeError('Git blob verification failed: '+entry['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            target.chmod(0o755 if entry['mode'] == '100755' else 0o644)
            return str(target.relative_to(out/'assets')), hashlib.sha256(data).hexdigest()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            hashes.update(dict(pool.map(download, files)))
    save_json(out / 'assets.sha256.json', hashes)


async def pin_image(image, out, mirror=''):
    rc, raw, _ = await command('docker', 'image', 'inspect', image, check=False)
    transport = image
    if rc:
        if mirror:
            transport = mirror.rstrip('/')+'/'+image.removeprefix('docker.io/')
        await command('docker', 'pull', transport, timeout=3600,
            output=out/'downloads/image-pulls'/(hashlib.sha256(image.encode()).hexdigest()[:16]+'.log'))
        _, raw, _ = await command('docker', 'image', 'inspect', transport)
    info = json.loads(raw)[0]
    return dict(image_id=info['Id'], original_image=image, transport=transport,
        repo_digests=info.get('RepoDigests', []), size_bytes=info['Size'])


async def prepare_images(plan):
    from harbor.models.task.task import Task
    out = freeze_plan(plan)
    cfg = plan['config']['execution']
    filename = out / 'images.json'
    images = read_json(filename) if filename.exists() else {}
    for ds in plan['datasets']:
        if ds['name'] == 'appworld':
            record = await pin_image(images.get('appworld', {}).get('image_id', cfg['appworld_image']), out, cfg['image_mirror'])
            record['original_image'] = images.get('appworld', {}).get('original_image', cfg['appworld_image'])
            images['appworld'] = record
            save_json(filename, images)
            continue
        for entry in ds['tasks']:
            key = ds['name']+'/'+entry['name']
            source = out/'assets'/key
            task = Task(source)
            image = task.config.environment.docker_image
            if not image:
                recipe = (source/'environment/Dockerfile').read_text()
                # The converted SWE recipes add uv and /logs to the official image.
                commands = [line.strip() for line in recipe.splitlines() if line.startswith(('RUN ', 'COPY ', 'ADD '))]
                if ds['name'] != 'swebench-verified' or commands != ['RUN curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh', 'RUN mkdir -p /logs']:
                    raise ValueError('Dataset needs an explicitly supported image recipe: '+key)
                image = re.search(r'^FROM\s+(\S+)', recipe, re.M).group(1)
            record = await pin_image(images.get(key, {}).get('image_id', image), out, cfg['image_mirror'])
            record['original_image'] = images.get(key, {}).get('original_image', image)
            if key in images and images[key]['image_id'] != record['image_id']:
                raise RuntimeError('Pinned task image changed')
            record.update(cpus=max(cfg['minimum_cpus'], task.config.environment.cpus or 1),
                memory_mb=max(cfg['minimum_memory_mb'], task.config.environment.memory_mb or 0),
                agent_timeout_sec=task.config.agent.timeout_sec,
                verifier_timeout_sec=task.config.verifier.timeout_sec)
            images[key] = record
            save_json(filename, images)
            target = out/'execution_assets'/key
            if not target.exists():
                shutil.copytree(source, target)
            text = (source/'task.toml').read_text()
            if re.search(r'^\s*docker_image\s*=', text, re.M):
                text = re.sub(r'docker_image\s*=\s*"[^"\n]+"', 'docker_image = "'+record['image_id']+'"', text)
            else:
                text = text.replace('[environment]', '[environment]\ndocker_image = "'+record['image_id']+'"', 1)
            (target/'task.toml').write_text(text)
    hashes = {str(p.relative_to(out/'execution_assets')): digest(p)
        for p in (out/'execution_assets').rglob('*') if p.is_file()}
    save_json(out/'execution-assets.sha256.json', hashes)


async def prepare_dependencies(plan):
    out = freeze_plan(plan)
    cfg = plan['config']['execution']
    binary = ROOT/'.runtime/bin'
    managed = out/'dependencies/python-managed'
    managed.mkdir(parents=True, exist_ok=True)
    for version in ['3.12.15', '3.13.5']:
        name = f'cpython-{version}-linux-x86_64-gnu'
        if not (managed/name).exists():
            shutil.copytree(ROOT/'.runtime/python'/name, managed/name)
    cache = out/'dependencies/uv-cache'
    cache.mkdir(exist_ok=True)
    (cache/'archive-v0').mkdir(exist_ok=True)
    env = {**os.environ, 'UV_CACHE_DIR': str(cache), 'UV_PYTHON_INSTALL_DIR': str(managed),
        'UV_PYTHON_DOWNLOADS': 'never', 'UV_INDEX_URL': cfg['pypi_index']}
    from .infra.dependency_policy import grading_indexes
    seen = set()
    for ds in plan['datasets']:
        if ds['name'] == 'swebench-verified':
            python = ROOT/'.runtime/python/cpython-3.12.15-linux-x86_64-gnu/bin/python3.12'
            venv = out/'dependencies/swe-parser'
            if not (venv/'bin/python').exists():
                await command(binary/'uv095/uv', 'venv', venv, '--python', python)
            await command(binary/'uv095/uv', 'pip', 'sync', '--python', venv/'bin/python',
                ROOT/'configs/swe-parser.requirements.lock.txt', env=env, timeout=1800)
            continue
        if ds['name'] != 'terminal-bench':
            continue
        for task in ds['tasks']:
            if task['name'] == 'qemu-alpine-ssh':
                from .infra.prepare_os_tools import prepare
                await asyncio.to_thread(prepare)
            if task['name'] == 'build-cython-ext':
                from .infra.prepare_git_inputs import prepare
                await asyncio.to_thread(prepare, out)
            script = (out/'assets/terminal-bench'/task['name']/'tests/test.sh').read_text().replace('\\\n', ' ')
            import shlex
            match = re.search(r'(?m)^uvx ([^\n]+)', script)
            if match:
                argv = shlex.split(match.group(1))
                if 'pytest' not in argv:
                    continue
                argv = argv[:argv.index('pytest')]+['pytest', '--version']
            else:
                match = re.search(r'(?m)^(?:uv )?pip install ([^\n]+)', script)
                if not match:
                    continue
                specs = shlex.split(match.group(1))
                argv = ['-p', '3.12', *[x for spec in specs for x in ['-w', spec]], 'pytest', '--version']
            signature = tuple(argv)
            if signature in seen:
                continue
            seen.add(signature)
            await command(binary/'uv095/uvx', *argv, env={**env, **grading_indexes(task['name'])}, timeout=1800,
                output=out/'downloads/dependencies'/(task['name']+'.log'))


async def prepare_appworld(plan):
    cfg = plan['config']['execution']
    if not any(ds['name'] == 'appworld' for ds in plan['datasets']):
        return
    python = Path(cfg['appworld_python'])
    if not python.exists():
        raise ValueError('Install AppWorld first: python -m harness_benchmark.install --appworld')
    root = Path(cfg['appworld_root'])
    root.mkdir(parents=True, exist_ok=True)
    await command(python.parent/'appworld', 'install', timeout=300)
    if not (root/'data/datasets').exists():
        await command(python.parent/'appworld', 'download', 'data', '--root', root, timeout=3600)
    from .config import catalog_for
    for ds in plan['datasets']:
        if ds['name'] != 'appworld':
            continue
        source = root/'data/datasets'/(ds['split']+'.txt')
        catalog = catalog_for(ds, plan['config'])
        if catalog.get('split_file_sha256') and digest(source) != catalog['split_file_sha256']:
            raise ValueError('AppWorld split file differs from pinned version')
        actual = set(source.read_text().split())
        if not {t['name'] for t in ds['tasks']} <= actual:
            raise ValueError('Selected AppWorld IDs are not in the downloaded split')


async def prepare(plan):
    out = freeze_plan(plan)
    if any((out/'cells').glob('*/started.json')):
        raise ValueError('Preparation is frozen after the first trial starts; use a new output_dir')
    download_tasks(plan)
    await prepare_appworld(plan)
    await prepare_images(plan)
    await prepare_dependencies(plan)
    print('Prepared selected tasks, images and grading dependencies; model calls: 0')
