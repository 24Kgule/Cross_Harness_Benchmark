"""Three native protocols and the AppWorld route, using only localhost mocks."""
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


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_protocols_authentication_limits_and_world_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seen = []
            async def upstream(request):
                seen.append((request.path, request.headers.get('Authorization'), await request.json()))
                if request.path.endswith('chat/completions'):
                    events = [dict(model='model-a', choices=[dict(delta={'content':'OK'})]),
                              dict(model='model-a', choices=[dict(delta={},finish_reason='stop')],
                                   usage=dict(prompt_tokens=2,completion_tokens=1))]
                elif request.path.endswith('messages'):
                    events = [dict(type='message_start',message=dict(model='model-a',usage=dict(input_tokens=2))),
                              dict(type='content_block_delta',delta=dict(type='text_delta',text='OK')),
                              dict(type='message_delta',usage=dict(output_tokens=1)),dict(type='message_stop')]
                else:
                    events = [dict(type='response.created',response=dict(model='model-a')),
                              dict(type='response.output_text.delta',delta='OK'),
                              dict(type='response.completed',response=dict(model='model-a',usage=dict(input_tokens=2,output_tokens=1)))]
                raw = ''.join('data: '+json.dumps(event)+'\r\n\r\n' for event in events).encode()
                return web.Response(body=raw, content_type='text/event-stream')
            async def world(request):
                self.assertEqual(request.headers['Authorization'], 'Bearer world-private-token')
                self.assertEqual(await request.json(), {'code':'print(2)'})
                return web.json_response({'output':'2'})
            upstream_app = web.Application()
            for route in ['chat/completions','messages','responses']:
                upstream_app.router.add_post('/v1/'+route, upstream)
            upstream_app.router.add_post('/execute', world)
            runners = []
            async def start(app):
                runner = web.AppRunner(app)
                runners.append(runner)
                await runner.setup()
                site = web.TCPSite(runner,'127.0.0.1',0)
                await site.start()
                return 'http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
            try:
                upstream_url = await start(upstream_app)
                (root/'trial.json').write_text(json.dumps(dict(bridge=upstream_url,token='world-private-token')))
                settings = APISettings('model-a',upstream_url+'/v1','mock-provider-secret',max_output_tokens=16,
                                       accepted_response_models=('model-a',))
                app = create_app(settings,'mock-master',root/'requests.jsonl',3,root)
                url = await start(app)
                headers = {'Authorization':'Bearer '+hmac.new(b'mock-master',b'trial','sha256').hexdigest()}
                async with aiohttp.ClientSession() as client:
                    async with client.post(url+'/t/trial/v1/chat/completions',json={}) as response:
                        self.assertEqual(response.status,401)
                    for route in ['chat/completions','messages','responses']:
                        body = dict(model='model-a',stream=True,max_output_tokens=50) if route=='responses' else dict(model='model-a',stream=True,max_tokens=50)
                        async with client.post(url+'/t/trial/v1/'+route,json=body,headers=headers) as response:
                            self.assertEqual(response.status,200)
                            self.assertIn('OK', await response.text())
                    async with client.post(url+'/t/trial/v1/responses',json=body,headers=headers) as response:
                        self.assertEqual(response.status,429)
                    async with client.post(url+'/t/trial/appworld/execute',json={'code':'print(2)'},headers=headers) as response:
                        self.assertEqual(await response.json(),{'output':'2'})
                self.assertEqual(len(seen),3)
                self.assertTrue(all(item[1]=='Bearer mock-provider-secret' for item in seen))
                self.assertEqual(seen[0][2]['max_tokens'],16)
                logs = [json.loads(line) for line in (root/'requests.jsonl').read_text().splitlines()]
                self.assertTrue(all(row['complete'] and row['model_echo_allowed'] and row['first_text_ms'] is not None for row in logs))
                self.assertNotIn('mock-provider-secret',(root/'requests.jsonl').read_text())
            finally:
                for runner in reversed(runners):
                    await runner.cleanup()


if __name__ == '__main__':
    unittest.main()
