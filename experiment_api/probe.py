"""Small real protocol probes. Never print or persist a provider credential."""
import argparse
import asyncio
import datetime
import json
from pathlib import Path
import time

import aiohttp

from .config import ROOT, load_settings, public_fingerprint


async def request(session, settings, protocol, body=None):
    headers = {'Authorization': 'Bearer ' + settings.api_key,
               'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
    if protocol == 'messages':
        headers.update({'x-api-key': settings.api_key, 'anthropic-version': '2023-06-01'})
    start = time.perf_counter()
    record = dict(protocol=protocol, requested_model=settings.model)
    try:
        method = session.get if protocol == 'models' else session.post
        kwargs = {} if protocol == 'models' else {'json': body}
        async with method(settings.endpoint(protocol), headers=headers, allow_redirects=False, **kwargs) as response:
            record['http_status'] = response.status
            record['content_type'] = response.headers.get('Content-Type', '')
            data = await response.json(content_type=None)
            if protocol == 'models':
                models = [x.get('id') for x in data.get('data', [])]
                record.update(model_listed=settings.model in models, model_count=len(models))
            else:
                record['response_model'] = data.get('model')
                record['usage'] = data.get('usage')
                if response.status == 200:
                    if protocol == 'chat':
                        message = (data.get('choices') or [{}])[0].get('message', {})
                        text = message.get('content') or ''
                        record['tool_calls'] = message.get('tool_calls') or []
                        record['finish_reason'] = (data.get('choices') or [{}])[0].get('finish_reason')
                    elif protocol == 'messages':
                        text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
                        record['tool_calls'] = [b for b in data.get('content', []) if b.get('type') == 'tool_use']
                        record['finish_reason'] = data.get('stop_reason')
                    else:
                        text = ''.join(c.get('text', '') for b in data.get('output', []) for c in b.get('content', []) if c.get('type') == 'output_text')
                        record['tool_calls'] = [b for b in data.get('output', []) if b.get('type') in ['function_call', 'custom_tool_call']]
                        record['finish_reason'] = data.get('status')
                    record.update(text=text[:200], has_text=bool(text),
                        model_echo_matches=data.get('model') == settings.model,
                        model_echo_allowed=data.get('model') in settings.accepted_response_models)
                else:
                    error = data.get('error', {})
                    record['error_type'] = error.get('type') if isinstance(error, dict) else type(error).__name__
                    message = error.get('message', '') if isinstance(error, dict) else str(error)
                    record['error_message'] = str(message).replace(settings.api_key, '[REDACTED]')[:350]
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as e:
        record['error_type'] = type(e).__name__
    record['duration_ms'] = round((time.perf_counter() - start) * 1000, 2)
    return record


def text_payload(settings, protocol):
    if protocol == 'chat':
        return dict(model=settings.model, messages=[dict(role='user', content='Reply with exactly OK.')],
                    max_tokens=128, stream=False, thinking={'type': 'disabled'})
    if protocol == 'messages':
        return dict(model=settings.model, messages=[dict(role='user', content='Reply with exactly OK.')],
                    max_tokens=128, stream=False, thinking={'type': 'disabled'})
    return dict(model=settings.model, input='Reply with exactly OK.', max_output_tokens=128, stream=False)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output')
    args = parser.parse_args()
    settings = load_settings()
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = Path(args.output) if args.output else ROOT / 'runs/probes' / stamp
    out.mkdir(parents=True, exist_ok=True)
    if (out / 'connectivity.json').exists():
        raise SystemExit('Connectivity evidence already exists; select a new output directory')
    timeout = aiohttp.ClientTimeout(total=60, connect=10, sock_read=45)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        records = await asyncio.gather(request(session, settings, 'models'), *(
            request(session, settings, p, text_payload(settings, p)) for p in ['chat', 'messages', 'responses']))
    report = dict(checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        configuration=settings.public(), configuration_sha256=public_fingerprint(settings),
        purpose='API connectivity only; not benchmark executions or verified provider billing',
        records=records)
    (out / 'connectivity.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    for r in records:
        print(json.dumps(r, ensure_ascii=False))
    print(json.dumps({'report': str(out / 'connectivity.json')}, ensure_ascii=False))
    if not all(r.get('http_status') == 200 and (
        r.get('model_listed') if r['protocol'] == 'models' else r.get('has_text') and r.get('model_echo_allowed')) for r in records):
        raise SystemExit(1)


if __name__ == '__main__':
    asyncio.run(main())
