"""Linux process-tree and cgroup v2 sampling. CPU percent: 100 = one logical CPU."""
import asyncio
import json
import time
from pathlib import Path

import psutil


class ResourceMonitor:
    def __init__(self, output, pids, cgroup=None, interval=0.5):
        self.output, self.pids, self.cgroup, self.interval = Path(output), pids, cgroup, interval
        self.stopped = False
        self.cpu_seen = {}  # retain exited PID CPU counters, keyed by PID/create_time

    def processes(self):
        found = {}
        for pid in self.pids():
            try:
                p = psutil.Process(pid)
                for q in [p, *p.children(recursive=True)]:
                    found[q.pid] = q
            except psutil.Error:
                pass
        return found.values()

    def snapshot(self):
        rss = 0
        count = 0
        for p in self.processes():
            try:
                t = p.cpu_times()
                self.cpu_seen[(p.pid, p.create_time())] = t.user + t.system
                rss += p.memory_info().rss
                count += 1
            except psutil.Error:
                pass
        data = {"mono_sec": time.monotonic(), "time": time.time(),
                "process_tree_cpu_sec": sum(self.cpu_seen.values()), "process_tree_rss_bytes": rss,
                "process_count": count, "host_memory_available_bytes": psutil.virtual_memory().available,
                "host_cpu_percent": psutil.cpu_percent()}
        if self.cgroup:
            try:
                cpu = dict(line.split() for line in (self.cgroup / 'cpu.stat').read_text().splitlines())
                data.update(container_cpu_sec=int(cpu['usage_usec']) / 1e6,
                            container_memory_bytes=int((self.cgroup / 'memory.current').read_text()))
                peak = self.cgroup / 'memory.peak'
                if peak.exists():
                    data['container_memory_lifetime_peak_bytes'] = int(peak.read_text())
            except (OSError, KeyError):
                data['container_sample_missing'] = True
        return data

    async def run(self):
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open('w') as f:
            while not self.stopped:
                f.write(json.dumps(self.snapshot()) + '\n')
                f.flush()
                await asyncio.sleep(self.interval)
            f.write(json.dumps(self.snapshot()) + '\n')


def resource_summary(path):
    rows = [json.loads(l) for l in Path(path).read_text().splitlines()]
    result = {"sample_count": len(rows), "interval_sec": 0.5,
              "cpu_percent_unit": "100% = one logical CPU; may exceed 100%",
              "process_cpu_limit": "sampled counters retain exited processes; sub-interval children may be missed; RSS sum can double-count shared pages"}
    for label, cpu, mem in [('harness', 'process_tree_cpu_sec', 'process_tree_rss_bytes'),
                            ('container', 'container_cpu_sec', 'container_memory_bytes')]:
        valid = [r for r in rows if cpu in r and mem in r]
        pairs = [(a, b) for a, b in zip(rows, rows[1:]) if cpu in a and cpu in b and mem in a and mem in b]
        duration = sum(b['mono_sec'] - a['mono_sec'] for a, b in pairs)
        if not valid or duration <= 0:
            result[label] = None
            continue
        delta = sum(max(0, b[cpu] - a[cpu]) for a, b in pairs)
        result[label] = {
            "observed_wall_sec": duration, "cpu_sec": delta, "cpu_percent_mean": delta / duration * 100,
            "cpu_percent_peak": max(max(0, b[cpu] - a[cpu]) / (b['mono_sec'] - a['mono_sec']) * 100 for a, b in pairs),
            "memory_bytes_mean": sum((a[mem] + b[mem]) / 2 * (b['mono_sec'] - a['mono_sec']) for a, b in pairs) / duration,
            "memory_bytes_peak_sampled": max(r[mem] for r in valid),
        }
    return result
