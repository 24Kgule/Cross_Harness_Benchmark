"""Validate an experiment and freeze one shared, deterministic sample per dataset."""
import copy
import hashlib
import json
from pathlib import Path
import random
import re

from .common import ROOT, HARNESSES, read_json

DATASETS = ('swebench-verified', 'terminal-bench', 'appworld')


def positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def load_config(filename='configs/benchmark.json'):
    path = Path(filename).resolve()
    cfg = read_json(path)
    if not isinstance(cfg, dict):
        raise ValueError('Config must be a JSON object')
    if set(cfg) - {'schema_version', 'output_dir', 'harnesses', 'datasets', 'execution', 'api_config'}:
        raise ValueError('Unknown top-level config field')
    if cfg.get('schema_version') != 1:
        raise ValueError('schema_version must be 1')
    harnesses = cfg.get('harnesses')
    if not isinstance(harnesses, list) or not harnesses or not all(isinstance(h, str) for h in harnesses) or len(set(harnesses)) != len(harnesses) or set(harnesses) - set(HARNESSES):
        raise ValueError('harnesses must be a nonempty, unique subset of: '+', '.join(HARNESSES))
    output = cfg.get('output_dir')
    if not isinstance(output, str) or not output:
        raise ValueError('output_dir is required')
    cfg['output_dir'] = str((ROOT / output).resolve())
    if Path(cfg['output_dir']) == ROOT or ROOT.is_relative_to(Path(cfg['output_dir'])):
        raise ValueError('output_dir cannot contain the source checkout')
    for protected in ['configs', 'harness_benchmark', 'experiment_api', 'vendor', 'tests', '.git', '.runtime']:
        if Path(cfg['output_dir']).is_relative_to(ROOT/protected):
            raise ValueError('output_dir cannot overwrite source/runtime directories')
    if not isinstance(cfg.get('api_config', ''), str):
        raise ValueError('api_config must be a path string')
    cfg['api_config'] = str((ROOT / cfg.get('api_config', 'configs/api.active.json')).resolve())
    execution = cfg.setdefault('execution', {})
    if not isinstance(execution, dict):
        raise ValueError('execution must be an object')
    defaults = dict(workers=2, minimum_cpus=2, minimum_memory_mb=6144,
        max_requests=50000, agent_network='task-default', image_mirror='',
        pypi_index='https://pypi.org/simple', hf_endpoint=None,
        appworld_root='datasets/appworld', appworld_python='.venv-appworld/bin/python',
        appworld_image='python:3.12-slim', appworld_timeout_sec=1800,
        appworld_max_interactions=200, appworld_code_timeout_sec=60)
    if set(execution) - set(defaults):
        raise ValueError('Unknown execution field: '+', '.join(sorted(set(execution)-set(defaults))))
    for key, value in defaults.items():
        execution.setdefault(key, value)
    for key in ['workers', 'minimum_cpus', 'minimum_memory_mb', 'max_requests',
                'appworld_timeout_sec', 'appworld_max_interactions', 'appworld_code_timeout_sec']:
        positive_int(execution[key], 'execution.'+key)
    if execution['agent_network'] not in ['task-default', 'isolated']:
        raise ValueError('agent_network must be task-default or isolated')
    for key in ['image_mirror', 'pypi_index', 'appworld_root', 'appworld_python', 'appworld_image']:
        if not isinstance(execution[key], str) or (key != 'image_mirror' and not execution[key]):
            raise ValueError('execution.'+key+' must be a path/URL/image string')
    if execution['hf_endpoint'] is not None and not isinstance(execution['hf_endpoint'], str):
        raise ValueError('hf_endpoint must be a URL string or null')
    for key in ['appworld_root', 'appworld_python']:
        execution[key] = str((ROOT / execution[key]).resolve())
    seen = set()
    if not isinstance(cfg.get('datasets'), list) or not cfg['datasets']:
        raise ValueError('datasets must be a nonempty array')
    for ds in cfg['datasets']:
        if not isinstance(ds, dict):
            raise ValueError('Each dataset must be an object')
        if set(ds) - {'name', 'enabled', 'sample_size', 'seed', 'split', 'task_ids'}:
            raise ValueError('Unknown dataset field')
        name = ds.get('name')
        if not isinstance(name, str) or name not in DATASETS or name in seen:
            raise ValueError('Unknown or duplicate dataset: '+str(name))
        seen.add(name)
        ds.setdefault('enabled', True)
        if type(ds['enabled']) is not bool:
            raise ValueError('enabled must be boolean')
        ds.setdefault('seed', 20261029)
        if type(ds['seed']) is not int:
            raise ValueError('seed must be an integer')
        if name == 'appworld':
            ds.setdefault('split', 'test_normal')
            if ds['split'] not in ['train', 'dev', 'test_normal', 'test_challenge']:
                raise ValueError('Unsupported AppWorld split')
        elif ds.get('split') is not None:
            raise ValueError('split is only configurable for AppWorld')
        if not ds['enabled']:
            continue
        ids = ds.get('task_ids')
        if ids is not None:
            if not isinstance(ids, list) or not ids or not all(isinstance(t, str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', t) for t in ids) or len(ids) != len(set(ids)):
                raise ValueError('task_ids must contain unique, safe task names')
            if ds.get('sample_size') is not None:
                raise ValueError('Choose task_ids or sample_size, not both')
        elif ds.get('sample_size') != 'all':
            positive_int(ds.get('sample_size'), name+'.sample_size')
    if not any(ds['enabled'] for ds in cfg['datasets']):
        raise ValueError('Enable at least one dataset')
    return cfg


def catalog_for(ds, cfg):
    catalog = read_json(ROOT / 'configs/datasets' / (ds['name']+'.json'))
    if ds['name'] == 'appworld' and ds['split'] != catalog['split']:
        source = Path(cfg['execution']['appworld_root']) / 'data/datasets' / (ds['split']+'.txt')
        if not source.exists():
            raise ValueError('Download AppWorld data before selecting split '+ds['split'])
        names = [line.strip() for line in source.read_text().splitlines() if line.strip()]
        catalog = dict(dataset='appworld', version=catalog['version'], split=ds['split'],
            population=len(names), tasks=[{'name': name} for name in names])
    tasks = catalog['tasks']
    if len({t['name'] for t in tasks}) != len(tasks):
        raise ValueError('Duplicate tasks in dataset catalog')
    return catalog


def make_plan(cfg):
    datasets = []
    cells = []
    for ds in cfg['datasets']:
        if not ds['enabled']:
            continue
        catalog = catalog_for(ds, cfg)
        population = sorted(catalog['tasks'], key=lambda task: task['name'])
        by_name = {task['name']: task for task in population}
        if ds.get('task_ids') is not None:
            if set(ds['task_ids']) - set(by_name):
                raise ValueError('task_ids contain tasks outside '+ds['name'])
            chosen = [by_name[name] for name in ds['task_ids']]
        else:
            random.Random(str(ds['seed'])+':'+ds['name']+':'+str(ds.get('split', ''))).shuffle(population)
            count = len(population) if ds['sample_size'] == 'all' else ds['sample_size']
            if count > len(population):
                raise ValueError(f'{ds["name"]}: sample_size {count} exceeds population {len(population)}')
            chosen = population[:count]
        datasets.append(dict(name=ds['name'], version=catalog['version'], split=ds.get('split'),
            seed=ds['seed'], population=len(population), tasks=copy.deepcopy(chosen)))
        for i, task in enumerate(chosen):
            harnesses = cfg['harnesses'][i % len(cfg['harnesses']):] + cfg['harnesses'][:i % len(cfg['harnesses'])]
            for harness in harnesses:
                cells.append(dict(index=len(cells), dataset=ds['name'], task=task['name'], harness=harness))
    plan = dict(schema_version=1, config=cfg, datasets=datasets, harnesses=cfg['harnesses'], cells=cells)
    plan['plan_sha256'] = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return plan
