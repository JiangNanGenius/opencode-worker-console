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
    return {'$schema': 'https://opencode.ai/config.json', 'agent': agents,
               'permission': 'allow' if auto else 'ask',
               'share': 'disabled', 'autoupdate': False, 'snapshot': False,
               'formatter': False}


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
