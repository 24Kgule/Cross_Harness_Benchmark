"""Pass native API protocols to the active relay and record latency and usage.

The relay tariff is unknown. Costs remain null; official prices and historical
monetary ledgers are deliberately not used for this new provider.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import time
import uuid

import aiohttp
from aiohttp import web

from .observation import Observation
from .config import ROOT, load_settings, public_fingerprint

PRIVATE = ROOT / 'experiment_api/private'


def create_app(settings, proxy_key, log_path, max_requests, routes_directory=None):
    if type(max_requests) is not int or max_requests <= 0:
        raise ValueError('A positive request limit is required for this gateway run')
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    app = web.Application(client_max_size=24 * 1024 * 1024)
    state = dict(dispatched=0, identity_frozen=False, access_blocked=False)
    app['benchmark_state'] = state
    config_hash = public_fingerprint(settings)

    async def context(app):
        timeout = aiohttp.ClientTimeout(total=1800, connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout, auto_decompress=False) as session:
            state['client'] = session
            yield
    app.cleanup_ctx.append(context)

    def authenticate(request):
        trial = request.match_info['trial']
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', trial):
            raise web.HTTPBadRequest(text='Invalid trial identifier')
        expected = hmac.new(proxy_key.encode(), trial.encode(), 'sha256').hexdigest()
        given = request.headers.get('x-api-key') or request.headers.get('Authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(expected, given):
            raise web.HTTPUnauthorized()
        return trial

    async def handle(request):
        trial = authenticate(request)
        if state['access_blocked']:
            raise web.HTTPServiceUnavailable(text='Relay access failed; dispatch is stopped')
        if state['identity_frozen']:
            raise web.HTTPServiceUnavailable(text='Relay response model did not match the pinned identifiers')
        if state['dispatched'] >= max_requests:
            raise web.HTTPTooManyRequests(text='This gateway run reached its request limit')
        protocol = 'chat' if request.path.endswith('/chat/completions') else 'responses' if request.path.endswith('/responses') else 'messages'
        body = await request.json()
        if body.get('model') != settings.model:
            raise web.HTTPBadRequest(text='Only the active relay model is allowed')
        original_limits = {k: body[k] for k in ['max_tokens', 'max_completion_tokens', 'max_output_tokens'] if k in body}
        if any(type(n) is not int or n <= 0 for n in original_limits.values()):
            raise web.HTTPBadRequest(text='Invalid output token limit')
        key = 'max_output_tokens' if protocol == 'responses' else 'max_completion_tokens' if 'max_completion_tokens' in body else 'max_tokens'
        tokens = min(original_limits.values(), default=settings.max_output_tokens)
        tokens = min(tokens, settings.max_output_tokens)
        body[key] = tokens
        if protocol == 'chat' and body.get('stream'):
            body.setdefault('stream_options', {})['include_usage'] = True
        payload = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        headers = {'Authorization': 'Bearer ' + settings.api_key,
                   'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
        if protocol == 'messages':
            headers.update({'x-api-key': settings.api_key,
                            'anthropic-version': request.headers.get('anthropic-version', '2023-06-01')})
            if request.headers.get('anthropic-beta'):
                headers['anthropic-beta'] = request.headers['anthropic-beta']
        request_id = uuid.uuid4().hex
        start = time.perf_counter()
        obs = Observation(protocol, start)
        # request.json() yields, so recheck the quota immediately before dispatch.
        if state['dispatched'] >= max_requests:
            raise web.HTTPTooManyRequests(text='This gateway run reached its request limit')
        if state['identity_frozen']:
            raise web.HTTPServiceUnavailable(text='Relay response model identifier changed')
        if state['access_blocked']:
            raise web.HTTPServiceUnavailable(text='Relay access failed; dispatch is stopped')
        state['dispatched'] += 1
        status = failure = response = None
        clean_end = False
        try:
            async with state['client'].post(settings.endpoint(protocol), data=payload,
                                         headers=headers, allow_redirects=False) as upstream:
                status = upstream.status
                if status in [401, 402, 403]:
                    state['access_blocked'] = True
                response = web.StreamResponse(status=status, headers={
                    'Content-Type': upstream.headers.get('Content-Type', 'application/json'),
                    'X-Benchmark-Request-ID': request_id})
                await response.prepare(request)
                is_sse = 'text/event-stream' in upstream.headers.get('Content-Type', '')
                pending, chunks = b'', []
                async for chunk in upstream.content.iter_any():
                    await response.write(chunk)
                    if not is_sse:
                        chunks.append(chunk)
                        continue
                    pending += chunk
                    while match := re.search(rb'\r?\n\r?\n', pending):
                        obs.sse(pending[:match.start()].decode('utf-8', errors='replace'))
                        pending = pending[match.end():]
                    if len(pending) > 8 * 1024 * 1024:
                        raise ValueError('Oversized SSE event')
                if is_sse and pending.strip():
                    obs.sse(pending.decode('utf-8', errors='replace'))
                if not is_sse:
                    data = json.loads(b''.join(chunks))
                    obs.read(data)
                    obs.complete = status == 200 and bool(obs.usage)
                clean_end = True
                await response.write_eof()
                return response
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, ValueError) as error:
            failure = type(error).__name__
            if response is None:
                raise web.HTTPBadGateway(text='Active relay request failed')
            response.force_close()
            return response
        finally:
            identity_ok = bool(obs.models) and obs.models.issubset(set(settings.accepted_response_models))
            if (obs.models and not identity_ok) or (status == 200 and clean_end and not obs.models):
                state['identity_frozen'] = True
            schemas = json.dumps(body.get('tools', []), ensure_ascii=False, sort_keys=True)
            record = dict(request_id=request_id, trial=trial, provider='relay', protocol=protocol,
                api_configuration_sha256=config_hash, requested_model=settings.model,
                response_models=sorted(obs.models), model_echo_allowed=identity_ok,
                identity_verification_scope='Reported model identifier only; not independent weight verification',
                started_at=time.time() - (time.perf_counter() - start),
                status=status, duration_ms=(time.perf_counter() - start) * 1000,
                first_event_ms=obs.first_event_ms, first_content_ms=obs.first_content_ms,
                first_text_ms=obs.first_text_ms, first_tool_ms=obs.first_tool_ms,
                usage=obs.usage, complete=obs.complete, clean_end=clean_end,
                original_output_limits=original_limits, max_output_tokens=tokens,
                reasoning_effort=body.get('reasoning_effort'), thinking=body.get('thinking'),
                reasoning=body.get('reasoning'), output_config=body.get('output_config'),
                parallel_tool_calls=body.get('parallel_tool_calls'),
                tool_schema_sha256=hashlib.sha256(schemas.encode()).hexdigest(),
                tool_names=[t.get('name') or t.get('function', {}).get('name') or t.get('type') for t in body.get('tools', [])],
                selected_tools=list(obs.selected_tools.values()), malformed_events=obs.malformed_events,
                estimated_cny=None, pricing_status='relay_tariff_not_provided', failure=failure,
                gateway_revision='active_relay_native_passthrough_v1')
            with log_path.open('a') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')

    for route in ['/v1/chat/completions', '/v1/messages', '/v1/responses']:
        app.router.add_post('/t/{trial}' + route, handle)

    async def appworld(request):
        trial = authenticate(request)
        if routes_directory is None:
            raise web.HTTPNotFound()
        path = Path(routes_directory) / (trial + '.json')
        if not path.is_file():
            raise web.HTTPNotFound()
        info = json.loads(path.read_text())
        async with state['client'].post(info['bridge'] + '/execute', json=await request.json(),
            headers={'Authorization': 'Bearer ' + info['token']}, allow_redirects=False) as upstream:
            return web.Response(body=await upstream.read(), status=upstream.status, content_type='application/json')
    app.router.add_post('/t/{trial}/appworld/execute', appworld)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--log', required=True)
    parser.add_argument('--max-requests', type=int, required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--unix-socket')
    args = parser.parse_args()
    settings = load_settings()
    path = Path(args.log).resolve()
    if path.exists():
        raise SystemExit('Refusing to append a new relay gateway run to existing request evidence')
    PRIVATE.mkdir(parents=True, exist_ok=True)
    PRIVATE.chmod(0o700)
    master = PRIVATE / 'master.txt'
    if not master.exists():
        master.write_text(secrets.token_hex(32))
        master.chmod(0o600)
    routes = PRIVATE / 'routes'
    routes.mkdir(exist_ok=True)
    async def serve():
        socket = Path(args.unix_socket).resolve() if args.unix_socket else None
        if socket and socket.exists():
            raise SystemExit('Relay socket already exists; inspect the previous run before reusing it')
        if socket:
            socket.parent.mkdir(parents=True, exist_ok=True)
        app = create_app(settings, master.read_text(), path, args.max_requests, routes)
        runner = web.AppRunner(app)
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in [signal.SIGINT, signal.SIGTERM]:
            loop.add_signal_handler(sig, stopped.set)
        await runner.setup()
        try:
            site = web.UnixSite(runner, str(socket)) if socket else web.TCPSite(runner, args.host, args.port)
            await site.start()
            if socket:
                socket.chmod(0o666)
            await stopped.wait()
        finally:
            await runner.cleanup()
            if socket:
                socket.unlink(missing_ok=True)
    asyncio.run(serve())


if __name__ == '__main__':
    main()
