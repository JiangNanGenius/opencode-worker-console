#!/usr/bin/env python3
"""Launch OpenCode with a private runtime overlay, preserving interactive config."""
import json
import os
from pathlib import Path
import bailian_pool
from common import STATE, config, init
import routing
from worker import instructions
from providers import ARK_PROVIDER, BAILIAN_PROVIDER, ark_provider, bailian_provider, valid_key_file

# Runtime declarations for the native Kimi provider. K3.1 Pro is the reserved
# Ultra model; the legacy K3 entry is retired and is never declared for new work.
KIMI_MODELS = {
    'kimi-for-coding': ('Kimi for Coding', 32768),
    'k3.1-pro': ('Kimi K3.1 Pro', 131072),
}


def kimi_gate_settings(c):
    """Optional loopback gate settings; a missing module or disabled block is None."""
    try:
        import kimi_gate
    except ImportError:
        return None
    return kimi_gate.settings(c)


def runtime_overlay(c):
    auto = c.get('auto_approve', True)
    agents = {}
    for name, profile in c['profiles'].items():
        # Disabled and retired profiles keep their records for history but are not
        # registered as new-task agents. `steps` is deliberately never sent:
        # OpenCode then iterates until the model stops or the user interrupts.
        if profile.get('enabled') is False or routing.is_retired_model(profile.get('model')):
            continue
        agents[name] = {'description': 'General-purpose delegated worker: investigate, summarize, implement, analyze and verify.',
                        'mode': 'primary', 'model': profile['model'], **({'variant': profile['variant']} if profile.get('variant') else {}),
                        'prompt': instructions(auto),
                        'permission': 'allow' if auto else {'*': 'deny', 'read': 'allow', 'glob': 'allow', 'grep': 'allow',
                                       'list': 'allow', 'lsp': 'allow', 'todowrite': 'allow'}}
    overlay = {'$schema': 'https://opencode.ai/config.json', 'agent': agents,
               'permission': 'allow' if auto else 'ask',
               'share': 'disabled', 'autoupdate': False, 'snapshot': False,
               'formatter': False}
    if c.get('memory', {}).get('enabled') is True:
        # Exactly one memory plugin is loaded: the OpenViking plugin after cutover,
        # the legacy opencode-mem plugin otherwise. Both capture automatically, so
        # loading them together would duplicate every session capture.
        backend = c['memory'].get('backend') if isinstance(c['memory'], dict) else None
        overlay['plugin'] = ['@openviking/opencode-plugin'] if backend == 'openviking' else ['opencode-mem']
    # Keep the original preset/auth ID stable when the upstream catalog renames it
    # to kimi-code-plan-cn/global. This uses the same existing kimi.com endpoint
    # and stored credential; no key migration or extra destination is involved.
    kimi_models = {}
    for name, profile in c['profiles'].items():
        if profile.get('enabled') is False or routing.is_retired_model(profile.get('model')):
            continue
        model = str(profile.get('model') or '')
        provider, _, model_id = model.partition('/')
        if provider == 'kimi-for-coding' and model_id:
            kimi_models[model_id] = KIMI_MODELS.get(model_id, (model_id, 32768))
    if kimi_models:
        options = {'baseURL': 'https://api.kimi.com/coding/v1'}
        gate = kimi_gate_settings(c)
        if gate is not None:
            # The optional loopback gate authenticates the local caller with the
            # owner-only server password and injects the real inference key from
            # OpenCode's private auth store upstream. The provider/auth identity is
            # unchanged, so credential identity, billing circuits and usage
            # attribution continue to bind to the real Kimi inference key.
            password_file = (STATE / 'server-password').expanduser().absolute()
            password_path = str(password_file)
            if not valid_key_file(password_path):
                raise ValueError('server-password path is not usable as an OpenCode key file')
            options = {'baseURL': 'http://127.0.0.1:%d/v1' % gate['port'],
                       'apiKey': '{file:' + password_path + '}'}
        overlay['provider'] = {'kimi-for-coding': {
            'name': 'Kimi for Coding', 'npm': '@ai-sdk/openai-compatible',
            'options': options,
            'models': {model: {
                'name': name, 'reasoning': True, 'tool_call': True,
                'interleaved': {'field': 'reasoning_content'},
                'modalities': {'input': ['text', 'image'], 'output': ['text']},
                'limit': {'context': 1048576, 'output': output},
                'variants': {level: {'reasoningEffort': level} for level in ('low', 'high', 'max')},
            } for model, (name, output) in kimi_models.items()},
        }}
    if any(p.get('enabled') is not False and str(p.get('model', '')).startswith(ARK_PROVIDER + '/')
           and not routing.is_retired_model(p.get('model')) for p in c['profiles'].values()):
        overlay.setdefault('provider', {})[ARK_PROVIDER] = ark_provider()
    if any(p['model'].startswith(BAILIAN_PROVIDER + '/') for p in c['profiles'].values()):
        # The Alibaba provider is not in OpenCode's built-in catalog, so the
        # runtime declares it. With the optional credential pool enabled, the
        # provider points at the loopback proxy and authenticates with the
        # existing owner-only server password file. The proxy injects a real
        # slot key; OpenCode never sees it. Otherwise the previous single
        # key-file behavior is preserved exactly.
        pool = bailian_pool.pool_settings(c)
        if pool is not None:
            password_file = (STATE / 'server-password').expanduser().absolute()
            password_path = str(password_file)
            if not valid_key_file(password_path):
                raise ValueError('server-password path is not usable as an OpenCode key file')
            local_base = 'http://127.0.0.1:%d' % pool['port']
            overlay.setdefault('provider', {})[BAILIAN_PROVIDER] = bailian_provider(
                password_path, base_url=local_base)
        else:
            key_files = c.get('provider_api_key_files')
            key_file = key_files.get(BAILIAN_PROVIDER) if isinstance(key_files, dict) else None
            overlay.setdefault('provider', {})[BAILIAN_PROVIDER] = bailian_provider(key_file)
    return overlay


def main():
    init()
    c = config()
    overlay = runtime_overlay(c)
    env = dict(os.environ)
    env['OPENCODE_SERVER_PASSWORD'] = (STATE / 'server-password').read_text().strip()
    env['OPENCODE_CONFIG_CONTENT'] = json.dumps(overlay)
    env['OPENCODE_DISABLE_AUTOUPDATE'] = '1'
    if c.get('memory', {}).get('enabled') is True and c['memory'].get('backend') == 'openviking':
        import openviking_service
        openviking_service.write_runtime_config()
        # The plugin and its MCP proxy authenticate with the owner-only data key
        # through the environment; no key is written into configuration files.
        key = openviking_service.api_key()
        if key:
            env['OPENVIKING_API_KEY'] = key
    # Pure mode prevents arbitrary project plugins from entering delegated execution.
    # A managed memory plugin is an explicitly configured runtime plugin, so enabled
    # installs start without --pure and keep the plugin list pinned in our overlay.
    port = c['server_url'].rsplit(':', 1)[1]
    os.chdir(STATE)
    argv = [c['opencode_binary'], 'serve', '--hostname', '127.0.0.1', '--port', port]
    if c.get('memory', {}).get('enabled') is not True:
        argv.append('--pure')
    os.execve(c['opencode_binary'], argv, env)


if __name__ == '__main__':
    main()
