#!/usr/bin/env python3
"""Stdin -> AppWorld execute, compatible with secret-filtering Shell tools."""
import json
import os
from pathlib import Path
import sys
import urllib.request

if sys.argv[1:] not in [[], ['execute']]:
    raise SystemExit('Usage: appworld execute < code.py')
config = json.loads(Path('/logs/agent/effective-command.json').read_text())['config_env']
url = os.environ.get('BENCH_APPWORLD_URL') or config['BENCH_APPWORLD_URL']
token = os.environ.get('BENCH_TRIAL_TOKEN') or config['BENCH_TRIAL_TOKEN']
request = urllib.request.Request(url, data=json.dumps({'code': sys.stdin.read()}).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer '+token})
with urllib.request.urlopen(request, timeout=90) as response:
    print(json.load(response)['output'])
