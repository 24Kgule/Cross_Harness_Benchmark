"""Stop native CLI trees before exposing grader assets, including on timeout."""
SCRIPT=r'''
import json,os,signal,time
from pathlib import Path
prefixes=('/opt/bench/native_entry.py','/opt/bench/npm/node_modules/','/opt/bench/dsh/node_modules/', '/opt/bench/bin/codex','/opt/bench/relay.cjs')
def snapshot():
 rows={}
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   args=[a.decode(errors='replace') for a in (p/'cmdline').read_bytes().split(b'\0') if a]
   parent=int((p/'stat').read_text().split(') ',1)[1].split()[1])
   executable=os.readlink(p/'exe')
   rows[int(p.name)]=(parent,args,executable)
  except (OSError,ValueError):pass
 return rows
rows=snapshot();roots={pid for pid,(parent,args,exe) in rows.items() if pid!=os.getpid() and any(a.startswith(prefixes) for a in args)}
def task_service(args,exe):
 name=Path(exe).name
 if name=='nginx' or name.startswith('qemu-system-'):return True
 if exe.startswith(('/usr/lib/postfix/','/usr/lib/mailman3/')):return True
 if name.startswith('python'):
  if '-m' in args and args.index('-m')+1<len(args) and args[args.index('-m')+1].startswith('mailman.'):return True
  if len(args)>1 and 'mailman' in args[1].lower() and Path(args[1]).name in ['master','runner','mailman','master.py','runner.py']:return True
 return False
services={pid for pid,(parent,args,exe) in rows.items() if task_service(args,exe)}
protected=set(services)
while True:
 extra={pid for pid,(parent,args,exe) in rows.items() if parent in protected and pid not in protected}
 if not extra:break
 protected.update(extra)
owned=set(roots)
while True:
 extra={pid for pid,(parent,args,exe) in rows.items() if parent in owned and pid not in owned}
 if not extra:break
 owned.update(extra)
owned.difference_update(protected);owned.discard(1);owned.discard(os.getpid())
for pid in sorted(owned,reverse=True):
 try:os.kill(pid,signal.SIGKILL)
 except ProcessLookupError:pass
# Ignore zombies: they cannot execute code or read tests. SIGKILL delivery and
# kernel I/O completion can exceed 50 ms under the benchmark's filesystem load.
wait_started=time.monotonic()
while True:
 remaining=[]
 for pid in owned:
  try:
   state=Path(f'/proc/{pid}/stat').read_text().split(') ',1)[1].split()[0]
   if state!='Z':remaining.append(pid)
  except OSError:pass
 if not remaining or time.monotonic()-wait_started>=10:break
 time.sleep(.05)
print(json.dumps(dict(at=time.time(),rule_revision='task_services_v3',termination_wait_sec=time.monotonic()-wait_started,native_roots=sorted(roots),terminated_pids=sorted(owned),preserved_service_roots=sorted(services),preserved_service_pids=sorted(protected),remaining_live_pids=remaining,passed=not remaining)))
assert not remaining
'''
