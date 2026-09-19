#!/usr/bin/env python3
"""Launch OpenCode with a private runtime overlay, preserving interactive config."""
import json
import os
from pathlib import Path
from common import STATE, config, init
from worker import instructions


def runtime_overlay(c):
    auto = c.get('auto_approve', True)
    agents = {}
    for name, profile in c['profiles'].items():
        # `steps` is deliberately never sent: OpenCode then iterates until the
        # model stops or the user interrupts. Any legacy max_steps is inert.
        agents[name] = {'description': 'General-purpose delegated worker: investigate, summarize, implement, analyze and verify.',
                        'mode': 'primary', 'model': profile['model'], **({'variant': profile['variant']} if profile.get('variant') else {}),
                        'prompt': instructions(auto),
                        'permission': 'allow' if auto else {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow',
                                       'list': 'allow', 'lsp': 'allow', 'todowrite': 'allow'}}
    overlay = {'$schema': 'https://opencode.ai/config.json', 'agent': agents,
               'permission': 'allow' if auto else 'ask',
               'share': 'disabled', 'autoupdate': False, 'snapshot': False,
               'formatter': False}
    # Keep the original preset/auth ID stable when the upstream catalog renames it
    # to kimi-code-plan-cn/global. This uses the same existing kimi.com endpoint
    # and stored credential; no key migration or extra destination is involved.
    if any(p['model'].startswith('kimi-for-coding/') for p in c['profiles'].values()):
        overlay['provider'] = {'kimi-for-coding': {
            'name': 'Kimi for Coding', 'npm': '@ai-sdk/openai-compatible',
            'options': {'baseURL': 'https://api.kimi.com/coding/v1'},
            'models': {model: {
                'name': name, 'reasoning': True, 'tool_call': True,
                'interleaved': {'field': 'reasoning_content'},
                'modalities': {'input': ['text', 'image'], 'output': ['text']},
                'limit': {'context': 1048576, 'output': output},
                'variants': {level: {'reasoningEffort': level} for level in ('low', 'high', 'max')},
            } for model, name, output in (
                ('kimi-for-coding', 'Kimi for Coding', 32768), ('k3', 'Kimi K3', 131072))},
        }}
    return overlay


def main():
    init()
    c = config()
    overlay = runtime_overlay(c)
    env = dict(os.environ)
    env['OPENCODE_SERVER_PASSWORD'] = (STATE / 'server-password').read_text().strip()
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(overlay)
    env['OPENCODE_DISABLE_AUTOUPDATE'] = '1'
    # Avoid external plugins in delegated execution; project instructions still apply.
    port = c['server_url'].rsplit(':', 1)[1]
    os.chdir(STATE)
    os.execve(c['opencode_binary'], [c['opencode_binary'], 'serve', '--hostname', '127.0.0.1',
                                   '--port', port, '--pure'], env)


if __name__ == '__main__':
    main()
