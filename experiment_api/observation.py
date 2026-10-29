"""Protocol observations, extracted unchanged from the sealed collector."""
import json
import time

class Observation:
    def __init__(self, protocol, start):
        self.protocol, self.start = protocol, start
        self.usage, self.models = {}, set()
        self.first_event_ms = self.first_content_ms = self.first_text_ms = None
        self.complete = False
        self.malformed_events = 0
        self.selected_tools = {}
        self.first_tool_ms = None

    def read(self, item):
        now = (time.perf_counter() - self.start) * 1000
        if self.first_event_ms is None:
            self.first_event_ms = now
        message = item.get('response', {}) if self.protocol == 'responses' else item.get('message', {})
        model = item.get('model') or message.get('model')
        if model:
            self.models.add(model)
        self.usage.update(message.get('usage') or {})
        self.usage.update(item.get('usage') or {})
        if self.protocol == 'chat':
            deltas = [x.get('delta', x.get('message', {})) for x in item.get('choices', [])]
            for delta in deltas:
                for tc in delta.get('tool_calls', []):
                    key = str(tc.get('index', tc.get('id', len(self.selected_tools))))
                    if tc.get('function', {}).get('name'):
                        self.selected_tools[key] = tc['function']['name']
            text = any(x.get('content') for x in deltas)
            content = any(x.get('content') or x.get('reasoning_content') or x.get('tool_calls') for x in deltas)
            if any(x.get('finish_reason') for x in item.get('choices', [])):
                self.complete = True
        elif self.protocol == 'responses':
            kind = item.get('type', '')
            output_item = item.get('item') or {}
            if kind == 'response.output_item.added' and output_item.get('type') in ['function_call', 'custom_tool_call']:
                self.selected_tools[str(item.get('output_index', output_item.get('id')))] = output_item.get('name')
            text = kind == 'response.output_text.delta' and bool(item.get('delta'))
            content = text or (kind in ['response.reasoning_text.delta',
                'response.function_call_arguments.delta', 'response.custom_tool_call_input.delta']
                and bool(item.get('delta')))
            if kind in ['response.completed', 'response.incomplete']:
                self.complete = True
        else:
            delta = item.get('delta', {})
            block = item.get('content_block') or {}
            if item.get('type') == 'content_block_start' and block.get('type') == 'tool_use':
                self.selected_tools[str(item.get('index', block.get('id')))] = block.get('name')
            text = bool(delta.get('text'))
            content = bool(text or delta.get('thinking') or delta.get('partial_json'))
            if item.get('type') == 'message_stop':
                self.complete = True
        if content and self.first_content_ms is None:
            self.first_content_ms = now
        if text and self.first_text_ms is None:
            self.first_text_ms = now
        if self.selected_tools and self.first_tool_ms is None:
            self.first_tool_ms = now

    def sse(self, event):
        # Preserve multiline SSE semantics; do not log content or tool arguments.
        data = '\n'.join(x[5:].lstrip(' ') for x in event.splitlines() if x.startswith('data:'))
        if data.strip() == '[DONE]':
            return
        if data:
            try:
                self.read(json.loads(data))
            except (ValueError, TypeError, AttributeError):
                self.malformed_events += 1
