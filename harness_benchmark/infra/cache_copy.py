"""Copy immutable wheel data without host-specific cached tool environments."""

SCRIPT=r'''
import json,os,shutil,time
from pathlib import Path
started=time.monotonic()
source=Path('/opt/bench/verifier-cache');target=Path('/root/.cache/uv')
target.mkdir(parents=True,exist_ok=True)
seen={};count=0;copied_bytes=0;linked=0;skipped=[];rewritten=0;dropped=0
def copy_file(src,dst):
 global count,copied_bytes,linked
 st=os.stat(src);identity=(st.st_dev,st.st_ino)
 if st.st_nlink>1 and identity in seen:
  if os.path.lexists(dst):os.unlink(dst)
  os.link(seen[identity],dst);linked+=1
 else:
  shutil.copy2(src,dst);copied_bytes+=st.st_size
  if st.st_nlink>1:seen[identity]=dst
 count+=1
 return dst
def copy_entry(src,dst):
 # A native Agent may already have populated this cache. Never follow its
 # destination symlinks or let copytree reject an existing source symlink.
 if os.path.lexists(dst) and (dst.is_symlink() or src.is_symlink() or src.is_dir()!=dst.is_dir()):
  if dst.is_dir() and not dst.is_symlink():shutil.rmtree(dst)
  else:dst.unlink()
 if src.is_symlink():
  dst.symlink_to(os.readlink(src));return
 if src.is_dir():
  dst.mkdir(exist_ok=True)
  for child in src.iterdir():copy_entry(child,dst/child.name)
  shutil.copystat(src,dst,follow_symlinks=False)
 else:copy_file(src,dst)
for entry in source.iterdir():
 if entry.name in ['environments-v2','interpreter-v4','.lock','archive-v0']:continue
 copy_entry(entry,target/entry.name)
(target/'archive-v0').mkdir(exist_ok=True)
for entry in (source/'archive-v0').iterdir():
 if (entry/'pyvenv.cfg').is_file():skipped.append(entry.name);continue
 copy_entry(entry,target/'archive-v0'/entry.name)
for entry in target.rglob('*'):
 if not entry.is_symlink():continue
 raw=os.readlink(entry)
 if '/archive-v0/' not in raw:continue
 suffix=raw.split('/archive-v0/',1)[1]
 destination=target/'archive-v0'/suffix
 entry.unlink()
 if destination.exists():
  entry.symlink_to(os.path.relpath(destination,entry.parent));rewritten+=1
 else:dropped+=1
print(json.dumps(dict(exit_code=0,revision='idempotent-cache-overlay-v2',elapsed_sec=time.monotonic()-started,files=count,bytes_copied=copied_bytes,hardlinks_preserved=linked,host_tool_environments_skipped=len(skipped),archive_symlinks_rebased=rewritten,stale_archive_symlinks_dropped=dropped,shared_cache_modified=False)))
'''
