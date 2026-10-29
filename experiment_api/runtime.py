"""Generate public model metadata and isolated mounts for future experiments."""
import argparse
import json
from pathlib import Path

from .support import VERSIONS, runtime_overlay as frozen_overlay
from .config import ROOT, load_settings, public_fingerprint
from .gateway import PRIVATE


def write_once(path, data):
    text = json.dumps(data, ensure_ascii=False, indent=2) + '\n'
    if path.exists() and path.read_text() != text:
        raise ValueError('Refusing to replace a previously generated runtime configuration')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def prepare_runtime(settings=None, socket_directory=None):
    settings = settings or load_settings()
    fingerprint = public_fingerprint(settings)
    folder = ROOT / '.runtime/public' / fingerprint
    config = {**settings.public(), 'configuration_sha256': fingerprint,
              'harness_versions': VERSIONS}
    write_once(folder / 'api-runtime.json', config)
    catalog = json.loads((ROOT / 'configs/codex.models.template.json').read_text())
    catalog['models'] = [m for m in catalog['models'] if m.get('slug') == 'benchmark-model']
    if len(catalog['models']) != 1:
        raise ValueError('The configured catalog entry must be unambiguous')
    catalog['models'][0]['slug'] = settings.model
    catalog['models'][0]['display_name'] = settings.model
    catalog['models'][0]['context_window'] = settings.context_tokens
    catalog['models'][0]['max_context_window'] = settings.context_tokens
    catalog['models'][0]['effective_context_window_percent'] = 95
    write_once(folder / 'codex.models.json', catalog)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)
    socket_directory = Path(socket_directory) if socket_directory else PRIVATE / 'socket'
    socket_directory.mkdir(parents=True, exist_ok=True)
    overlay = frozen_overlay()
    for volume in overlay['services']['main']['volumes']:
        if volume['target'] == '/opt/bench/native_entry.py':
            volume['target'] = '/opt/bench/native_entry.frozen.py'
        elif volume['target'] == '/opt/bench/codex.models.json':
            volume['source'] = str(folder / 'codex.models.json')
        elif volume['target'] == '/run/bench-gateway':
            volume['source'] = str(socket_directory)
    overlay['services']['main']['volumes'].extend([
        dict(type='bind', source=str(ROOT / 'experiment_api/native_entry.py'),
             target='/opt/bench/native_entry.py', read_only=True),
        dict(type='bind', source=str(folder / 'api-runtime.json'),
             target='/opt/bench/api-runtime.json', read_only=True),
    ])
    write_once(folder / 'runtime-overlay.json', overlay)
    return folder, overlay


def main():
    argparse.ArgumentParser().parse_args()
    settings = load_settings()
    folder, _ = prepare_runtime(settings)
    print(json.dumps({'configuration': settings.public(), 'runtime_directory': str(folder),
        'harbor_agent': 'experiment_api.harbor_agent:RelayNativeAgent',
        'overlay': str(folder / 'runtime-overlay.json')}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
