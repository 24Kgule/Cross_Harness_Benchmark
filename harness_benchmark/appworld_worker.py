"""One private AppWorld process per trial; task containers never mount its data."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import signal


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--task', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--root', required=True)
    parser.add_argument('--experiment', required=True)
    parser.add_argument('--seed', required=True, type=int)
    parser.add_argument('--max-interactions', required=True, type=int)
    parser.add_argument('--code-timeout', required=True, type=int)
    args = parser.parse_args()
    os.environ['APPWORLD_ROOT'] = args.root
    from appworld import AppWorld
    world = AppWorld(task_id=args.task, experiment_name=args.experiment,
        max_interactions=args.max_interactions, timeout_seconds=args.code_timeout,
        load_ground_truth=True, ground_truth_mode='minimal', random_seed=args.seed)
    args.output.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer '+secret:
                self.send_error(401)
                return
            if self.path not in ['/execute', '/evaluate']:
                self.send_error(404)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/execute':
                    result = {'output': world.execute(body['code'])}
                else:
                    world.save_state()
                    result = world.evaluate(suppress_errors=True).to_dict()
                    (args.output/'evaluation.json').write_text(json.dumps(result, indent=2))
                data = json.dumps(result).encode()
                self.send_response(200)
            except Exception as error:
                data = json.dumps({'error': type(error).__name__}).encode()
                self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    prompt = f'''Complete this AppWorld task using the appworld execute Shell command.
Task: {world.task.instruction}
Supervisor: {json.dumps(dict(world.task.supervisor))}
Apps: {json.dumps(world.task.app_descriptions)}
Use `appworld execute` with Python code on stdin. Python variables persist across calls.
Example: appworld execute <<'PY'
print(apis.api_docs.show_app_descriptions())
PY
Inspect APIs with apis.api_docs.show_api_descriptions(app_name="...") and
apis.api_docs.show_api_doc(app_name="...", api_name="..."). Account credentials are
available via apis.supervisor.show_account_passwords(). Use apis.<app>.<api>(...).
Only this command interacts with the apps; the working container has a separate filesystem.
Preserve unrelated state. Finish with apis.supervisor.complete_task(status="success").
If the task asks a question, persist its answer using complete_task(status="success", answer="...").
A final natural-language reply does not store the official answer.'''
    ready = args.output/'ready.json'
    ready.write_text(json.dumps(dict(bridge=f'http://127.0.0.1:{server.server_port}', token=secret, prompt=prompt)))
    ready.chmod(0o600)
    def terminate(*_):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        world.close()
        ready.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
