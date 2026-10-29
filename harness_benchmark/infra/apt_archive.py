"""Fallback only HTTP404 Debian security files to archive, checking APT SHA256."""
SCRIPT=r'''
import hashlib,json,os,shlex,ssl,subprocess,urllib.error,urllib.parse,urllib.request
from pathlib import Path
packages=json.loads(os.environ['TB2_GRADER_APT_PACKAGES'])
plan=subprocess.check_output(['apt-get','--print-uris','-y','install',*packages],text=True)
artifacts=[]
for line in plan.splitlines():
 if not line.startswith("'"):continue
 uri,cache_name,size,*rest=shlex.split(line)
 if '/debian-security/' not in uri:continue
 try:
  with urllib.request.urlopen(urllib.request.Request(uri,method='HEAD'),timeout=30):pass
  continue
 except urllib.error.HTTPError as error:
  if error.code!=404:raise
 decoded=urllib.parse.unquote(cache_name);package,version,arch=decoded.removesuffix('.deb').rsplit('_',2)
 metadata=subprocess.check_output(['apt-cache','show',package+'='+version],text=True)
 candidates=[]
 for block in metadata.split('\n\n'):
  fields=dict(x.split(': ',1) for x in block.splitlines() if ': ' in x and not x.startswith(' '))
  if fields.get('Version')==version and fields.get('Architecture')==arch and urllib.parse.unquote(uri).endswith('/'+fields.get('Filename','__missing__')):candidates.append(fields)
 assert candidates,'No signed-index metadata for '+cache_name
 assert len({x['SHA256'] for x in candidates})==1
 fields=candidates[0];expected=fields['SHA256'];target=Path('/var/cache/apt/archives')/cache_name
 cached=target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==expected
 archive='https://archive.debian.org/debian-security/'+fields['Filename']
 if not cached:
  context=ssl.create_default_context(cafile='/etc/ssl/certs/ca-certificates.crt')
  with urllib.request.urlopen(archive,timeout=90,context=context) as response:data=response.read()
  assert len(data)==int(size) and hashlib.sha256(data).hexdigest()==expected,'Archive artifact differs from signed APT index'
  target.parent.mkdir(parents=True,exist_ok=True);temporary=target.with_suffix('.download');temporary.write_bytes(data);temporary.replace(target)
 artifacts.append(dict(package=package,version=version,original_uri=uri,archive_uri=archive,sha256=expected,bytes=int(size),cached=cached))
print(json.dumps(dict(model_calls=0,scope='Declared official grader OS dependencies only; fallback HTTP404 files with unchanged signed APT metadata and package versions',artifacts=artifacts)))
'''
