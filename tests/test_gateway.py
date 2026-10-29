"""Local upstream tests: no external API calls or real credentials."""
import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import unittest

import aiohttp
from aiohttp import web

from experiment_api.config import APISettings
from experiment_api.gateway import create_app


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.upstream_requests = []
        self.upstream_model = 'target-model'
        async def upstream(request):
            self.upstream_requests.append((request.headers.get('Authorization'), await request.json()))
            return web.json_response(dict(model=self.upstream_model,
                choices=[dict(message=dict(content='OK'), finish_reason='stop')],
                usage=dict(prompt_tokens=2, completion_tokens=1)))
        app = web.Application()
        app.router.add_post('/v1/chat/completions', upstream)
        self.upstream_runner = web.AppRunner(app)
        await self.upstream_runner.setup()
        self.upstream_site = web.TCPSite(self.upstream_runner, '127.0.0.1', 0)
        await self.upstream_site.start()
        port = self.upstream_site._server.sockets[0].getsockname()[1]
        settings = APISettings('target-model', f'http://127.0.0.1:{port}/v1', 'new-relay-credential',
                               accepted_response_models=('target-model',))
        self.log = Path(self.directory.name) / 'requests.jsonl'
        self.gateway_runner = web.AppRunner(create_app(settings, 'local-proxy-master', self.log, 3))
        await self.gateway_runner.setup()
        self.gateway_site = web.TCPSite(self.gateway_runner, '127.0.0.1', 0)
        await self.gateway_site.start()
        port = self.gateway_site._server.sockets[0].getsockname()[1]
        self.url = f'http://127.0.0.1:{port}/t/check/v1/chat/completions'
        token = hmac.new(b'local-proxy-master', b'check', 'sha256').hexdigest()
        self.headers = dict(Authorization='Bearer ' + token)
        self.session = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.session.close()
        await self.gateway_runner.cleanup()
        await self.upstream_runner.cleanup()
        self.directory.cleanup()

    async def post(self, model='target-model'):
        async with self.session.post(self.url, headers=self.headers,
            json=dict(model=model, messages=[dict(role='user', content='OK')], max_tokens=32)) as response:
            await response.read()
            return response.status

    async def test_wrong_model_is_rejected_before_upstream_dispatch(self):
        self.assertEqual(await self.post('historical-model'), 400)
        self.assertEqual(self.upstream_requests, [])

    async def test_current_credential_is_used_and_cost_remains_unknown(self):
        self.assertEqual(await self.post(), 200)
        self.assertEqual(self.upstream_requests[0][0], 'Bearer new-relay-credential')
        record = json.loads(self.log.read_text().splitlines()[0])
        self.assertTrue(record['model_echo_allowed'])
        self.assertIsNone(record['estimated_cny'])
        self.assertEqual(record['usage']['completion_tokens'], 1)
        self.assertNotIn('new-relay-credential', self.log.read_text())

    async def test_response_model_change_freezes_subsequent_dispatch(self):
        self.upstream_model = 'other-model'
        self.assertEqual(await self.post(), 200)
        self.assertEqual(await self.post(), 503)
        self.assertEqual(len(self.upstream_requests), 1)


if __name__ == '__main__':
    unittest.main()
