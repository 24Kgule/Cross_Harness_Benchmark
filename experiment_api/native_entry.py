"""Apply the active relay model to the frozen six-harness launcher."""
import importlib.util
import json
import os
from pathlib import Path
import sys


def main():
    public = json.loads(Path('/opt/bench/api-runtime.json').read_text())
    spec = importlib.util.spec_from_file_location('frozen_benchmark_native', '/opt/bench/native_entry.frozen.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.MODEL = public['model']
    module.CONTEXT = public['requested_context_tokens']
    module.MAX_OUTPUT = public['requested_max_output_tokens']
    os.environ['OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX'] = str(public['requested_max_output_tokens'])
    Path('/logs/agent/api-identity.json').write_text(json.dumps(public, indent=2))
    return module.main()


if __name__ == '__main__':
    sys.exit(main())
