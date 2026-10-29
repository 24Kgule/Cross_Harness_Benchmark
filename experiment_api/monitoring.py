"""Task cgroup monitoring; independent of historical live runners."""
import asyncio
import json
from pathlib import Path

from .resource_monitor import ResourceMonitor, resource_summary

async def command(*args):
    process=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
    stdout,stderr=await process.communicate()
    if process.returncode:raise RuntimeError("Container inspection failed")
    return process.returncode,stdout.decode(),stderr.decode()

async def monitor_container(cid,path):
    _,raw,_=await command('docker','inspect',cid)
    info=json.loads(raw)[0];pid=info['State']['Pid']
    cg=next(s.split(':',2)[2] for s in Path(f'/proc/{pid}/cgroup').read_text().splitlines() if s.startswith('0::'))
    cgpath=Path('/sys/fs/cgroup')/cg.lstrip('/')
    def pids():
        try:return [int(p) for p in (cgpath/'cgroup.procs').read_text().split()]
        except OSError:return [pid]
    monitor=ResourceMonitor(path,pids,cgpath)
    future=asyncio.create_task(monitor.run())
    return monitor,future
