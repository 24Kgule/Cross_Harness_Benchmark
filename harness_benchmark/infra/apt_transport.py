"""Reachable grader-only APT transport; keep repository signatures enabled."""
SCRIPT=r'''
from pathlib import Path
import json
mapping={'http://deb.debian.org/debian-security':'https://mirrors.tuna.tsinghua.edu.cn/debian-security','https://deb.debian.org/debian-security':'https://mirrors.tuna.tsinghua.edu.cn/debian-security','http://deb.debian.org/debian':'https://mirrors.tuna.tsinghua.edu.cn/debian','https://deb.debian.org/debian':'https://mirrors.tuna.tsinghua.edu.cn/debian','http://security.debian.org/debian-security':'https://mirrors.tuna.tsinghua.edu.cn/debian-security','http://archive.ubuntu.com/ubuntu':'https://mirrors.tuna.tsinghua.edu.cn/ubuntu','http://security.ubuntu.com/ubuntu':'https://mirrors.tuna.tsinghua.edu.cn/ubuntu'}
ca_bundle=Path('/etc/ssl/certs/ca-certificates.crt').is_file()
if not ca_bundle:mapping={old:new.replace('https://mirrors.tuna.tsinghua.edu.cn/','http://mirrors.tuna.tsinghua.edu.cn/') for old,new in mapping.items()}
changed=[]
for p in [Path('/etc/apt/sources.list'),*Path('/etc/apt/sources.list.d').glob('*.sources'),*Path('/etc/apt/sources.list.d').glob('*.list')]:
 if not p.is_file():continue
 before=p.read_text();after=before
 for old,new in mapping.items():after=after.replace(old,new)
 if before!=after:p.write_text(after);changed.append(str(p))
print(json.dumps({'changed_source_paths':changed,'signature_verification_unchanged':True,'ca_bundle_available':ca_bundle,'phase':'grader only; after Agent stop'}))
'''
