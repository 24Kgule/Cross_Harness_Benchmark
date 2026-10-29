"""Protocol usage and disjoint wall-time interval metrics."""
import collections
import datetime
import json
from .common import jsonl

def stamp(value):
    return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp() if value else None


def tokens(event):
    u=event.get('usage') or {}
    if event['protocol']=='chat':
        return (u.get('prompt_tokens',0),u.get('prompt_cache_hit_tokens',u.get('prompt_tokens_details',{}).get('cached_tokens',0)),u.get('completion_tokens',0))
    if event['protocol']=='responses':
        return (u.get('input_tokens',0),u.get('input_tokens_details',{}).get('cached_tokens',0),u.get('output_tokens',0))
    hit=u.get('cache_read_input_tokens',0)
    return (u.get('input_tokens',0)+hit+u.get('cache_creation_input_tokens',0),hit,u.get('output_tokens',0))


def phases(events,start,end):
    edges=collections.defaultdict(lambda:[0,0]);edges[start];edges[end]
    for e in events:
        a=max(start,e['started_at']);b=min(end,e['started_at']+e['duration_ms']/1000)
        if b<=a:continue
        first=e.get('first_content_ms')
        split=min(b,max(a,e['started_at']+first/1000)) if first is not None else b
        edges[a][0]+=1;edges[split][0]-=1;edges[split][1]+=1;edges[b][1]-=1
    result=[0.,0.,0.];active=[0,0];last=start
    for t,delta in sorted(edges.items()):
        result[1 if active[1]>0 else 0 if active[0]>0 else 2]+=t-last
        active=[active[i]+delta[i] for i in [0,1]];last=t
    return result


def first_text(path,start):
    timeline=jsonl(path/'agent/output-timeline.jsonl')
    native=[]
    source=path/'agent/stdout.jsonl'
    for line in source.read_text().splitlines() if source.exists() else []:
        try:
            event=json.loads(line)
            if isinstance(event, dict): native.append(event)
        except ValueError: pass
    if len(timeline)!=len(native):return None
    for t,e in zip(timeline,native):
        text=False
        if e.get('type')=='text':text=bool(e.get('text') or (e.get('part') or {}).get('text'))
        if e.get('role')=='assistant' and isinstance(e.get('content'),str):text=bool(e['content'])
        item=e.get('item') or {}
        if item.get('type')=='agent_message':text=bool(item.get('text'))
        stream=e.get('event') or {}
        if stream.get('type')=='content_block_delta':text=bool((stream.get('delta') or {}).get('text'))
        if (e.get('assistantMessageEvent') or {}).get('type')=='text_delta':text=bool(e['assistantMessageEvent'].get('delta'))
        if text:return max(0,t['time']-start)
    return None

