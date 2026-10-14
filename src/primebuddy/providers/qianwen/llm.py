"""千问文本与工具调用协议；不负责执行工具或重试业务。"""
import json
import urllib.request
from primebuddy.providers.http import NoRedirect


def request_message(messages, config, tools=None):
    body = {'model': config['model'], 'messages': messages, 'stream': False,
            'enable_thinking': False, 'temperature': 0, 'max_tokens': 2000}
    if tools is None:
        body['response_format'] = {'type': 'json_object'}
    else:
        body.update(tools=tools, parallel_tool_calls=False)
    request = urllib.request.Request(config['endpoint'], method='POST',
        data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + config['api_key']})
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('计划响应过大')
    choice = json.loads(raw)['choices'][0]
    if choice['finish_reason'] not in ('stop', 'tool_calls'):
        raise ValueError('计划响应不完整')
    message = choice['message']
    result = {'role': 'assistant', 'content': message.get('content') or ''}
    if message.get('tool_calls'):
        result['tool_calls'] = message['tool_calls']
    return result


def complete(messages, config):
    message = request_message(messages, config)
    if message.get('tool_calls'):
        raise ValueError('模型连接检查不应请求工具调用。')
    return json.loads(message['content'])


def chat(messages, tools, config):
    return request_message(messages, config, tools)
