"""Run a frozen native CLI inside a fresh task container, without global configs."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

NPM = '/opt/bench/npm/node_modules'
NODE = '/opt/bench/bin/node'
BIN = '/opt/bench/bin'
MODEL = None
CONTEXT = 1048576
MAX_OUTPUT = 65536
LOG = Path('/logs/agent')

def write(path, value, raw=False):
    path=Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(value if raw else json.dumps(value,indent=2))

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--harness',required=True)
    parser.add_argument('--trial',required=True)
    parser.add_argument('--token',required=True)
    args=parser.parse_args()
    if not MODEL:
        raise RuntimeError('Launch through experiment_api.native_entry with an explicit configured model')
    base='http://127.0.0.1:8765/t/'+args.trial
    env={**os.environ, 'PATH':BIN+':'+os.environ.get('PATH','/usr/local/bin:/usr/bin:/bin'),
         'BENCH_TRIAL_TOKEN':args.token, 'BENCH_APPWORLD_URL':base+'/appworld/execute',
         'TERM':'dumb','CI':'1','NO_COLOR':'1'}
    prompt=Path('/logs/agent/prompt.txt').read_text()
    relay=subprocess.Popen([NODE,'/opt/bench/relay.cjs'], stdout=subprocess.DEVNULL,stderr=open(LOG/'relay.stderr','w'))
    time.sleep(.2)
    command=[]
    if args.harness=='codex':
        write('/root/.codex/config.toml',f'''model = "{MODEL}"
model_provider = "deepseek"
model_reasoning_effort = "high"
model_catalog_json = "/opt/bench/codex.models.json"
web_search = "disabled"
model_context_window = {CONTEXT}
[model_providers.deepseek]
name = "deepseek"
base_url = "{base}/v1"
wire_api = "responses"
env_key = "BENCH_TRIAL_TOKEN"
[features]
multi_agent = false
enable_request_compression = false
responses_websockets = false
''',True)
        command=[BIN+'/codex','exec','--skip-git-repo-check','--dangerously-bypass-approvals-and-sandbox','--json','-']
    elif args.harness=='claude':
        env.update(ANTHROPIC_BASE_URL=base, ANTHROPIC_AUTH_TOKEN=args.token,
          ANTHROPIC_MODEL=MODEL, ANTHROPIC_DEFAULT_OPUS_MODEL=MODEL,
          ANTHROPIC_DEFAULT_SONNET_MODEL=MODEL,ANTHROPIC_DEFAULT_HAIKU_MODEL=MODEL,
          CLAUDE_CODE_SUBAGENT_MODEL=MODEL,CLAUDE_CODE_EFFORT_LEVEL='high',
          CLAUDE_CODE_AUTO_COMPACT_WINDOW=str(int(CONTEXT*.75)),CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(MAX_OUTPUT),
          CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',DISABLE_TELEMETRY='1',
          DISABLE_ERROR_REPORTING='1',DISABLE_AUTOUPDATER='1',IS_SANDBOX='1')
        command=[NPM+'/.bin/claude','-p','--verbose',
            '--output-format','stream-json','--include-partial-messages',
            '--dangerously-skip-permissions','--model',MODEL+('[1m]' if CONTEXT > 200000 else ''),'--effort','high',
            '--disallowedTools','Agent,Task,WebSearch,WebFetch']
    elif args.harness=='dsh':
        disabled=['tool-web','tool-subagent','tool-subagent-control','tool-subagent-list-agents',
          'tool-subagent-fork','tool-workflow','tool-ralph','tool-plugin-manager',
          'session-title-llm','session-log-deepseek','plugin-package-inventory-deepseek','session-telemetry-otel',
          'llm-deepseek-account']
        patch=[{'id':i,'disabled':True} for i in disabled]
        patch.extend([{'id':'agent-default-model','config':{'provider':'deepseek-official','model':MODEL,'reasoningEffort':'high'}},
            {'id':'llm-deepseek','config':{'baseURL':base,'maxTokens':MAX_OUTPUT,'reasoningEffort':'high',
              'models':[{'id':MODEL,'name':MODEL,'contextWindow':CONTEXT,'maxTokens':MAX_OUTPUT}]} }])
        write(LOG/'dsh.patch.json',patch)
        env.update(DSH_HOME=str(LOG/'dsh-home'),DEEPSEEK_API_KEY=args.token,DSH_PERMISSION_MODE='danger-full-access')
        command=['/opt/bench/dsh/node_modules/.bin/dsh','--profile','headless','--patch',str(LOG/'dsh.patch.json'),'--json','-']
    elif args.harness=='opencode':
        cfg={'$schema':'https://opencode.ai/config.json','model':'deepseek/'+MODEL,
          'provider':{'deepseek':{'npm':'@ai-sdk/openai-compatible','name':'DeepSeek',
            'options':{'baseURL':base+'/v1','apiKey':args.token},'models':{MODEL:{'id':MODEL,'name':MODEL,
              'reasoning':True,'limit':{'context':CONTEXT,'output':MAX_OUTPUT},
              'options':{'reasoningEffort':'high','thinking':{'type':'enabled'}}}}}},
          'permission':{'*':'allow','task':'deny','webfetch':'deny','websearch':'deny','question':'deny'},
          'share':'disabled','autoupdate':False}
        write(LOG/'opencode.json',cfg)
        env.update(OPENCODE_CONFIG=str(LOG/'opencode.json'),OPENCODE_DISABLE_DEFAULT_PLUGINS='true',
          OPENCODE_DISABLE_MODELS_FETCH='true',OPENCODE_DISABLE_AUTOUPDATE='true',
          XDG_DATA_HOME=str(LOG/'xdg-data'),XDG_STATE_HOME=str(LOG/'xdg-state'),
          XDG_CACHE_HOME=str(LOG/'xdg-cache'))
        command=[NPM+'/opencode-ai/bin/opencode.exe','run','--format','json',
          '--model','deepseek/'+MODEL,'--variant','high','--pure','--auto',prompt]
    elif args.harness=='pi':
        folder=LOG/'pi-home'
        write(folder/'models.json',{'providers':{'deepseek':{'baseUrl':base+'/v1',
          'api':'openai-completions','apiKey':args.token,'models':[{'id':MODEL,
          'name':MODEL,'reasoning':True,'contextWindow':CONTEXT,'maxTokens':MAX_OUTPUT,
          'compat':{'thinkingFormat':'deepseek','supportsReasoningEffort':True,
            'requiresReasoningContentOnAssistantMessages':True,'supportsDeveloperRole':False,
            'maxTokensField':'max_tokens'}}]}}})
        env.update(PI_CODING_AGENT_DIR=str(folder))
        command=[NPM+'/.bin/pi','--provider','deepseek','--model',MODEL,'--thinking','high',
          '--mode','json','--print','--no-mcp','--no-extensions','--no-skills','--offline',
          '--session-dir',str(LOG/'pi-sessions'),prompt]
    elif args.harness=='kimi':
        write(LOG/'kimi-home/config.toml','[tools]\nenabled = ["Bash", "Edit", "Glob", "Grep", "Read", "Write", "TaskList", "TaskOutput", "TaskStop", "WaitFor", "TodoList"]\n',True)
        env.update(KIMI_CODE_HOME=str(LOG/'kimi-home'),KIMI_MODEL_NAME=MODEL,
          KIMI_MODEL_BASE_URL=base,KIMI_MODEL_API_KEY=args.token,
          KIMI_MODEL_MAX_CONTEXT_SIZE=str(CONTEXT),KIMI_MODEL_MAX_OUTPUT_SIZE=str(MAX_OUTPUT),
          KIMI_MODEL_CAPABILITIES='thinking,tool_use',KIMI_MODEL_THINKING_EFFORT='high',
          KIMI_MODEL_PROVIDER_TYPE='anthropic',KIMI_MODEL_ADAPTIVE_THINKING='true',KIMI_DISABLE_TELEMETRY='true',
          KIMI_CODE_NO_AUTO_UPDATE='true')
        command=[NPM+'/.bin/kimi','--output-format','stream-json','--prompt',prompt]
    else:raise ValueError(args.harness)
    # All stored credentials are task-local proxy credentials, never provider keys.
    write(LOG/'effective-command.json',{'command':command,'config_env':{k:v for k,v in env.items() if k not in os.environ or os.environ.get(k)!=v}})
    try:
        with (LOG/'stdout.jsonl').open('wb') as out,(LOG/'stderr.log').open('wb') as err:
            process=subprocess.Popen(command,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err)
            began=time.monotonic()
            process.stdin.write(prompt.encode());process.stdin.close()
            with (LOG/'output-timeline.jsonl').open('w') as timeline:
                for line in process.stdout:
                    out.write(line);out.flush()
                    try:
                        event=json.loads(line)
                        nested=event.get('event') or {}
                        record={'elapsed_sec':time.monotonic()-began,'time':time.time(),
                          'type':event.get('type'),'subtype':event.get('subtype'),
                          'event_type':nested.get('type'),'role':event.get('role'),
                          'item_type':(event.get('item') or {}).get('type'),
                          'call_id':event.get('callId') or nested.get('index'),
                          'tool':event.get('tool') or nested.get('content_block',{}).get('name')}
                        timeline.write(json.dumps(record)+'\n');timeline.flush()
                    except (ValueError,TypeError,AttributeError):pass
            process.wait()
            return process.returncode
    finally:relay.terminate()

if __name__=='__main__':sys.exit(main())
