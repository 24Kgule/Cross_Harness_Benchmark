import datetime
import asyncio
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT / 'runs/default'
HARNESSES = ['codex', 'claude', 'dsh', 'opencode', 'pi', 'kimi']


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def jsonl(path):
    path = Path(path)
    return [json.loads(line) for line in path.read_text().splitlines() if line] if path.exists() else []


async def command(*arguments, timeout=180, check=True, output=None, env=None):
    process = await asyncio.create_subprocess_exec(
        *map(str, arguments), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    stdout, stderr = stdout.decode(errors='replace'), stderr.decode(errors='replace')
    if output is not None:
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(stdout + '\n' + stderr)
    if check and process.returncode:
        raise RuntimeError(' '.join(map(str, arguments[:3])) + ': ' + stderr[-1200:])
    return process.returncode, stdout, stderr
