"""Plot new benchmark runs or a user-supplied CSV; no API/runtime imports."""
import csv
import json
import math
from pathlib import Path
import statistics

from .common import HARNESSES, jsonl, now, read_json, save_json

NAMES = {'codex': 'Codex', 'claude': 'Claude Code', 'dsh': 'dsh', 'opencode': 'OpenCode', 'pi': 'Pi', 'kimi': 'Kimi Code'}
DATASETS = {'swebench-verified': 'SWE-Bench Verified', 'terminal-bench': 'Terminal-Bench 2.0', 'appworld': 'AppWorld'}
NUMERIC = {'official_score', 'agent_sec', 'prepare_sec', 'verifier_sec', 'total_sec', 'api_requests', 'input_tokens',
    'output_tokens', 'cached_input_tokens', 'api_wait_union_sec', 'api_generate_union_sec', 'non_api_sec',
    'container_cpu_sec', 'container_cpu_percent_mean', 'container_cpu_percent_peak', 'container_memory_bytes_mean',
    'container_memory_bytes_peak_sampled', 'first_native_json_text_sec', 'api_incomplete_responses', 'api_unknown_usage'}


def read_table(filename):
    with Path(filename).open() as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for key, value in row.items():
            if value == '':
                row[key] = None
            elif key in NUMERIC:
                row[key] = float(value)
                if not math.isfinite(row[key]):
                    raise ValueError('Non-finite numeric field: '+key)
    return rows


def write_table(path, rows):
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with Path(path).open('w') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def collect(out):
    from .metrics import tokens, phases, stamp, first_text
    out = Path(out)
    plan = read_json(out/'plan.json')
    requests = {}
    for file in sorted(out.glob('requests.*.jsonl')):
        for event in jsonl(file):
            requests.setdefault(event['trial'], []).append(event)
    rows = []
    for cell in plan['cells']:
        folder = out/'cells'/f'{cell["index"]:05d}'
        row = {**cell, 'official_score': None, 'status': 'not_started'}
        if (folder/'record.json').exists():
            record = read_json(folder/'record.json')
            row.update({k: v for k, v in record.items() if not isinstance(v, (list, dict))})
            container = record.get('resources', {}).get('resources', {}).get('container') or {}
            row.update({'container_'+key: value for key, value in container.items()})
            worker = (record.get('world_worker_resources') or {}).get('harness') or {}
            row.update({'world_worker_'+key: value for key, value in worker.items()})
            events = requests.get(record.get('trial'), [])
            row.update(api_requests=len(events), api_incomplete_responses=sum(not event.get('complete') for event in events),
                api_unknown_usage=sum(not event.get('usage') for event in events),
                input_tokens=sum(tokens(event)[0] for event in events),
                cached_input_tokens=sum(tokens(event)[1] for event in events), output_tokens=sum(tokens(event)[2] for event in events))
            result = record.get('result_path')
            if result:
                path = out/result
                trial = read_json(path)
                timing = trial.get('agent_execution') or {}
                start, end = stamp(timing.get('started_at')), stamp(timing.get('finished_at'))
                if start is not None and end is not None:
                    row.update(zip(['api_wait_union_sec', 'api_generate_union_sec', 'non_api_sec'], phases(events, start, end)))
                    row['first_native_json_text_sec'] = first_text(path.parent, start)
        rows.append(row)
    return rows


def median(rows, key):
    values = [float(row[key]) for row in rows if row.get(key) is not None and math.isfinite(float(row[key]))]
    return statistics.median(values) if values else None


def wilson(k, n):
    if n == 0:
        return [None, None]
    z = 1.95996398454
    p = k/n
    denominator = 1+z*z/n
    center = (p+z*z/(2*n))/denominator
    radius = z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/denominator
    return [max(0, center-radius), min(1, center+radius)]


def summarize(rows):
    if not rows:
        raise ValueError('No task rows to analyze')
    identities = [(r['dataset'], r['task'], r['harness']) for r in rows]
    if len(set(identities)) != len(identities):
        raise ValueError('Duplicate dataset/task/harness rows')
    if any(r.get('official_score') not in [None, 0, 1] for r in rows):
        raise ValueError('Expected official scores of 0, 1 or missing')
    datasets = list(dict.fromkeys(row['dataset'] for row in rows))
    harnesses = [h for h in HARNESSES if any(row['harness'] == h for row in rows)]
    if set(row['harness'] for row in rows) != set(harnesses):
        raise ValueError('Unknown harness in table')
    groups = []
    common = {}
    for dataset in datasets:
        present = [h for h in harnesses if any(r['dataset'] == dataset and r['harness'] == h for r in rows)]
        sets = [{r['task'] for r in rows if r['dataset'] == dataset and r['harness'] == h and r.get('official_score') is not None} for h in present]
        common[dataset] = sorted(set.intersection(*sets))
        for h in present:
            planned = [r for r in rows if r['dataset'] == dataset and r['harness'] == h]
            valid = [r for r in planned if r.get('official_score') is not None]
            successes = sum(r['official_score'] for r in valid)
            groups.append(dict(dataset=dataset, harness=h, planned=len(planned), valid=len(valid),
                successes=successes, success_rate=successes/len(valid) if valid else None,
                wilson95=wilson(successes, len(valid)),
                medians={key: median(valid, key) for key in ['agent_sec', 'api_requests', 'output_tokens',
                    'container_cpu_sec', 'container_memory_bytes_peak_sampled', 'first_native_json_text_sec']},
                phase_means={key: statistics.mean([r[key] for r in valid if r.get(key) is not None])
                    if any(r.get(key) is not None for r in valid) else None
                    for key in ['api_wait_union_sec', 'api_generate_union_sec', 'non_api_sec']}))
    return dict(generated_at=now(), datasets=datasets, harnesses=harnesses, groups=groups,
        common_valid_tasks=common, monetary_cost=None,
        interpretation='One execution per task/harness. Missing grading is not0. Rates use valid grades; common task IDs are listed separately.')


def figures(rows, summary, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'figure.dpi': 150, 'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    datasets = summary['datasets']
    harnesses = summary['harnesses']
    colors = dict(zip(HARNESSES, ['#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE', '#AA3377']))

    def save(fig, name):
        fig.tight_layout()
        fig.savefig(output/(name+'.png'), bbox_inches='tight')
        fig.savefig(output/(name+'.svg'), bbox_inches='tight')
        plt.close(fig)

    fig, axes = plt.subplots(1, len(datasets), figsize=(max(7, 6*len(datasets)), 4.8), squeeze=False)
    for ax, dataset in zip(axes[0], datasets):
        groups = [g for g in summary['groups'] if g['dataset'] == dataset]
        for i, group in enumerate(groups):
            rate = group['success_rate']
            if rate is None:
                ax.text(i, .05, 'No valid\ngrades', ha='center')
            else:
                lo, hi = group['wilson95']
                ax.bar(i, rate, color=colors[group['harness']], yerr=[[max(0, rate-lo)], [max(0, hi-rate)]], capsize=3)
                ax.text(i, hi+.025, f'{group["successes"]:g}/{group["valid"]}', ha='center')
        ax.set_xticks(range(len(groups)), [NAMES[g['harness']] for g in groups], rotation=30, ha='right')
        ax.set(title=DATASETS.get(dataset, dataset), ylim=(0, 1.1), ylabel='Success rate (95% Wilson interval)')
    save(fig, 'success_rates')
    for dataset in datasets:
        tasknames = list(dict.fromkeys(r['task'] for r in rows if r['dataset'] == dataset))
        hs = [h for h in harnesses if any(r['dataset'] == dataset and r['harness'] == h for r in rows)]
        by = {(r['task'], r['harness']): r.get('official_score') for r in rows if r['dataset'] == dataset}
        values = [[by.get((task, h)) for h in hs] for task in tasknames]
        matrix = np.array([[np.nan if value is None else value for value in row] for row in values])
        cmap = matplotlib.colors.ListedColormap(['#b00030', '#006b40']).with_extremes(bad='#cccccc')
        fig, ax = plt.subplots(figsize=(max(7, len(hs)*1.2), max(4, .3*len(tasknames))))
        ax.imshow(matrix, cmap=cmap, vmin=0, vmax=1, aspect='auto')
        ax.set_xticks(range(len(hs)), [NAMES[h] for h in hs])
        ax.set_yticks(range(len(tasknames)), tasknames, fontsize=max(5, 10-min(5, len(tasknames)//100)))
        if len(tasknames) <= 100:
            for y, row in enumerate(values):
                for x, value in enumerate(row):
                    ax.text(x, y, '—' if value is None else str(int(value)), ha='center', va='center', color='white', fontsize=8)
        ax.set_title(DATASETS.get(dataset, dataset)+' | green=1, red=0, gray=missing')
        save(fig, dataset+'_task_matrix')
    for name, fields in [('efficiency', [('agent_sec', 'Agent duration (seconds)'), ('api_requests', 'Model requests'), ('output_tokens', 'Observed output tokens')]),
                         ('resources', [('first_native_json_text_sec', 'First native text (seconds)'), ('container_cpu_sec', 'Container CPU time (seconds)'), ('container_memory_bytes_peak_sampled', 'Sampled peak memory (MiB)')])]:
        fig, axes = plt.subplots(len(datasets), 3, figsize=(15, 4*len(datasets)), squeeze=False)
        for dsindex, dataset in enumerate(datasets):
            for ax, (key, title) in zip(axes[dsindex], fields):
                values = [[r[key]/(1024**2 if 'memory' in key else 1) for r in rows
                    if r['dataset'] == dataset and r['harness'] == h and r.get('official_score') is not None and r.get(key) is not None] for h in harnesses]
                ax.boxplot([v or [np.nan] for v in values], tick_labels=[NAMES[h] for h in harnesses], showmeans=True)
                ax.tick_params(axis='x', rotation=30)
                ax.set_yscale('symlog', linthresh=1)
                ax.set_title(DATASETS.get(dataset, dataset)+'\n'+title)
        save(fig, name)
    fig, axes = plt.subplots(1, len(datasets), figsize=(max(7, 6*len(datasets)), 4.8), squeeze=False)
    for ax, dataset in zip(axes[0], datasets):
        groups = [g for g in summary['groups'] if g['dataset'] == dataset]
        bottom = np.zeros(len(groups))
        for key, label, color in [('api_wait_union_sec', 'Before first content', '#4477AA'),
                                 ('api_generate_union_sec', 'Streaming window', '#66CCEE'), ('non_api_sec', 'Outside requests', '#CCBB44')]:
            vals = np.array([g['phase_means'][key] if g['phase_means'][key] is not None else np.nan for g in groups])
            ax.bar(range(len(groups)), vals, bottom=bottom, label=label, color=color)
            bottom += np.nan_to_num(vals)
        ax.set_xticks(range(len(groups)), [NAMES[g['harness']] for g in groups], rotation=30, ha='right')
        ax.set(title=DATASETS.get(dataset, dataset), ylabel='Mean Agent wall time (seconds)')
    axes[0, -1].legend(loc='upper left', bbox_to_anchor=(1.01, 1))
    save(fig, 'time_components')


def analyze(rows, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    summary = summarize(rows)
    write_table(destination/'trials.csv', rows)
    save_json(destination/'summary.json', summary)
    figures(rows, summary, destination/'figures')
    lines = ['# Cross-harness benchmark', '', 'One configured model; one execution per task and harness.', '',
        '| Dataset | Harness | Valid / planned | Successes | Success rate | Median Agent seconds |', '|---|---|---:|---:|---:|---:|']
    for g in summary['groups']:
        rate = f'{g["success_rate"]:.1%}' if g['success_rate'] is not None else 'missing'
        duration = g['medians']['agent_sec']
        lines.append(f'| {g["dataset"]} | {NAMES[g["harness"]]} | {g["valid"]}/{g["planned"]} | {g["successes"]:g} | {rate} | {duration:.1f} |' if duration is not None else
            f'| {g["dataset"]} | {NAMES[g["harness"]]} | {g["valid"]}/{g["planned"]} | {g["successes"]:g} | {rate} | missing |')
    lines += ['', 'Rates use valid official grades. Missing grades are shown separately; task matrices expose coverage differences.',
        'The task is the sample unit. One execution cannot estimate all model sampling variation. Different datasets are not pooled.',
        'Before-first-content includes network, relay, queueing and model work; the streaming window is not pure decode.',
        'Outside-request time includes tools and local execution. CPU100% means one logical core; memory includes tools and caches.',
        'AppWorld host world-worker resources are recorded separately from the Agent container. Token usage may be incomplete; monetary cost is unknown.',
        '', '![Success rates](figures/success_rates.png)', '', '![Efficiency](figures/efficiency.png)', '',
        '![Resources](figures/resources.png)', '', '![Time components](figures/time_components.png)']
    for dataset in summary['datasets']:
        lines += ['', f'![{dataset} tasks](figures/{dataset}_task_matrix.png)']
    (destination/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(dict(rows=len(rows), datasets=summary['datasets'], harnesses=summary['harnesses'], destination=str(destination)), ensure_ascii=False))
    return summary
