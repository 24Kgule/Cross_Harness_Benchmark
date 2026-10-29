"""Config-driven benchmark CLI. Planning and plotting do not call a model."""
import argparse
import asyncio
import json
from pathlib import Path

from .config import load_config, make_plan


def main():
    parser = argparse.ArgumentParser(description='Evaluate one model across native harnesses')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ['plan', 'prepare', 'run', 'plot', 'doctor']:
        command = sub.add_parser(name)
        command.add_argument('--config', default='configs/benchmark.json')
        if name == 'plan':
            command.add_argument('--write', action='store_true', help='Freeze plan.json in output_dir')
        if name == 'prepare':
            command.add_argument('--assets-only', action='store_true', help='Download task files without images/dependencies')
        if name == 'plot':
            command.add_argument('--table', type=Path, help='Plot a supplied CSV instead of collecting the configured run')
            command.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.command == 'plot' and args.table:
        from .analyze import analyze, read_table
        analyze(read_table(args.table), args.output or args.table.parent/'analysis')
        return
    cfg = load_config(args.config)
    plan = make_plan(cfg)
    out = Path(cfg['output_dir'])
    if args.command == 'plan':
        if args.write:
            from .prepare import freeze_plan
            freeze_plan(plan)
        print(json.dumps(dict(harnesses=cfg['harnesses'], datasets=[dict(name=ds['name'], split=ds['split'],
            sample_size=len(ds['tasks']), population=ds['population']) for ds in plan['datasets']],
            planned_cells=len(plan['cells']), output_dir=str(out), plan_sha256=plan['plan_sha256'], model_calls=0), ensure_ascii=False, indent=2))
    elif args.command == 'prepare':
        from .prepare import prepare, download_tasks
        if args.assets_only:
            download_tasks(plan)
        else:
            asyncio.run(prepare(plan))
    elif args.command == 'run':
        from .run import run
        asyncio.run(run(plan))
    elif args.command == 'plot':
        from .analyze import analyze, collect
        analyze(collect(out), args.output or out/'analysis')
    elif args.command == 'doctor':
        from .common import ROOT
        from experiment_api.config import load_settings
        from .prepare import command
        settings = load_settings(config_file=cfg['api_config'])
        binaries = [ROOT/'.venv-harbor/bin/harbor', ROOT/'.runtime/bin/node', ROOT/'.runtime/bin/uv095/uv']
        missing = [str(p) for p in binaries if not p.exists()]
        if missing:
            raise SystemExit('Missing runtime; run python -m harness_benchmark.install: '+', '.join(missing))
        _, cgroup, _ = asyncio.run(command('docker', 'info', '--format', '{{.CgroupVersion}}'))
        if cgroup.strip() != '2':
            raise SystemExit('Resource monitoring requires Linux Docker with cgroup v2')
        asyncio.run(command('docker', 'compose', 'version'))
        print(json.dumps(dict(local_runtime='present', model=settings.model, provider='relay', api_requests=0,
            note='Local configuration only; run a connectivity probe separately if needed'), ensure_ascii=False))


if __name__ == '__main__':
    main()
