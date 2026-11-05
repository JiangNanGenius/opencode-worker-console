#!/usr/bin/env python3
"""Loopback relay enforcing the operator's live DeepSeek CNY spending floor."""
import common
import kimi_gate
import quota

DEFAULT_PORT = 51532
MODELS = frozenset(('deepseek-flash', 'deepseek-pro'))


def settings(c):
    block = c.get('deepseek_gate')
    if block is None:
        return None
    try:
        value = kimi_gate.settings({'kimi_gate': block})
    except ValueError as exc:
        raise ValueError(str(exc).replace('kimi_gate', 'deepseek_gate')) from None
    if value is not None and 'port' not in block:
        value['port'] = DEFAULT_PORT
    return value


class GateHandler(kimi_gate.GateHandler):
    server_version = 'DeepSeekGate/1'
    provider_id = 'deepseek'
    service_name = 'deepseek_gate'
    upstream_host = 'api.deepseek.com'
    upstream_prefix = '/v1'
    models = MODELS

    def gate_settings(self, c):
        return settings(c)

    def preflight(self, model, c):
        # The shared 60-second cache is credential-bound. A failed/old sample
        # never unlocks the floor. Every native tool-loop round enters here.
        return quota.allowed('deepseek', quota.refresh(), 'normal', c=c)


def create_server(c, port=None, host='127.0.0.1'):
    return kimi_gate.create_server(c, port, host, handler=GateHandler, settings_fn=settings)


def wait_ready(port, password=None, timeout=10):
    return kimi_gate.wait_ready(port, password, timeout, service_name='deepseek_gate')


def main():
    server = create_server(common.config())
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
