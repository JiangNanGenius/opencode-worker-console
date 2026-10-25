import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import public_access


class PublicAccessTests(unittest.TestCase):
    def setUp(self):
        self.gateway = patch.object(public_access, 'config', return_value={
            'console_passkey': {'enabled': True, 'rp_id': 'desk.example.com',
                                'public_origin': 'https://desk.example.com'}, 'console_public_port': 51529})
        self.gateway.start()
        self.addCleanup(self.gateway.stop)

    def test_ingress_only_targets_passkey_listener(self):
        frp, caddy = public_access.render({'domain': 'desk.example.com', 'server': 'frp.example.com', 'server_port': 5443})
        self.assertIn('localIP = "127.0.0.1"', frp)
        self.assertIn('transport.tls.enable = true', frp)
        self.assertIn('transport.heartbeatInterval = 30', frp)
        self.assertIn('transport.heartbeatTimeout = 90', frp)
        self.assertIn('transport.proxyProtocolVersion = "v2"', frp)
        self.assertIn('{{ .Envs.WORKERDESK_FRP_TOKEN }}', frp)
        self.assertIn('reverse_proxy 127.0.0.1:51529', caddy)
        self.assertNotIn('51528', caddy + frp)
        self.assertIn('default_bind 127.0.0.1', caddy)
        self.assertIn('header_up X-Forwarded-For {remote_host}', caddy)
        self.assertIn('disable_tlsalpn_challenge', caddy)
        self.assertIn('redir https://desk.example.com{uri} 308', caddy)

    def test_config_injection_is_rejected(self):
        for key, value in [('domain', 'x.example.com\nadmin off'), ('server', 'host"\nauth.token="x'), ('server_port', 0), ('domain', '..example.com')]:
            config = {'domain': 'desk.example.com', 'server': 'frp.example.com', 'server_port': 5443}
            config[key] = value
            with self.assertRaises(ValueError):
                public_access.render(config)

    def test_runtime_environment_does_not_inherit_provider_credentials(self):
        with patch.dict(os.environ, {'ARK_API_KEY': 'test-secret', 'OPENAI_API_KEY': 'test-secret2'}, clear=False):
            env = public_access._environment()
        self.assertNotIn('ARK_API_KEY', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('WORKERDESK_FRP_TOKEN', env)

    def test_failed_validator_never_echoes_expanded_secret(self):
        from subprocess import CompletedProcess
        with patch.object(public_access, 'settings', return_value={'domain': 'desk.example.com', 'server': 'frp.example.com', 'server_port': 5443}), \
             patch.object(public_access.keychain_secrets, 'get', return_value=b'secret-token'), \
             patch.object(public_access.subprocess, 'run', return_value=CompletedProcess([], 1, b'secret-token', b'secret-token')):
            with self.assertRaises(RuntimeError) as ctx:
                public_access.validate()
        self.assertNotIn('secret-token', str(ctx.exception))

    def test_configure_writes_private_reference_only_files(self):
        with tempfile.TemporaryDirectory() as root, patch.object(public_access, 'ROOT', Path(root)):
            public_access.configure('frp.example.com', 5443, 'desk.example.com')
            for name in ('frpc.toml', 'Caddyfile', 'settings.json'):
                self.assertEqual((Path(root) / name).stat().st_mode & 0o777, 0o600)
            self.assertIn('{{ .Envs.', (Path(root) / 'frpc.toml').read_text())

    def test_only_frpc_validator_receives_token(self):
        from subprocess import CompletedProcess
        with patch.object(public_access, 'settings', return_value={'domain': 'desk.example.com', 'server': 'frp.example.com', 'server_port': 5443}), \
             patch.object(public_access.keychain_secrets, 'get', return_value=b'secret-token'), \
             patch.object(public_access.subprocess, 'run', return_value=CompletedProcess([], 0)) as run:
            public_access.validate()
        self.assertEqual(run.call_args_list[0].kwargs['env']['WORKERDESK_FRP_TOKEN'], 'secret-token')
        self.assertNotIn('WORKERDESK_FRP_TOKEN', run.call_args_list[1].kwargs['env'])

    def test_mismatched_or_disabled_gateway_cannot_start_ingress(self):
        for gateway in ({}, {'console_passkey': {'enabled': False}},
                        {'console_passkey': {'enabled': True, 'public_origin': 'https://other.example.com', 'rp_id': 'other.example.com'}}):
            with patch.object(public_access, 'settings', return_value={'domain': 'desk.example.com', 'server': 'frp.example.com', 'server_port': 5443}), \
                 patch.object(public_access, 'config', return_value=gateway), \
                 patch.object(public_access.keychain_secrets, 'get') as get:
                with self.assertRaises(RuntimeError):
                    public_access.validate()
                get.assert_not_called()

    def test_stop_removes_autostart_files(self):
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as root, \
             patch.object(public_access, 'ROOT', Path(root) / 'state'), \
             patch.object(Path, 'home', return_value=Path(root)), \
             patch.object(public_access.subprocess, 'run', return_value=CompletedProcess([], 1)), \
             patch.object(public_access, 'status', return_value={}):
            folder = Path(root) / 'Library/LaunchAgents'; folder.mkdir(parents=True)
            for component in ('caddy', 'frpc'):
                (folder / (public_access.LABEL + component + '.plist')).write_bytes(b'old')
            public_access.launch('stop')
            self.assertEqual(list(folder.iterdir()), [])

    def test_partial_start_failure_withdraws_all_managed_jobs(self):
        from subprocess import CompletedProcess
        loaded = set()
        def execute(args, **kwargs):
            if args[1] == 'list':
                return CompletedProcess(args, 0 if args[2] in loaded else 1)
            if args[1] == 'bootout':
                loaded.discard(args[2].split('/')[-1]); return CompletedProcess(args, 0)
            if args[1] == 'bootstrap':
                component = Path(args[3]).stem
                if component.endswith('frpc'):
                    return CompletedProcess(args, 5)
                loaded.add(component); return CompletedProcess(args, 0)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(public_access, 'ROOT', Path(root) / 'state'), \
             patch.object(Path, 'home', return_value=Path(root)), \
             patch.object(public_access, 'validate'), \
             patch.object(public_access.subprocess, 'run', side_effect=execute):
            with self.assertRaises(RuntimeError):
                public_access.launch('start')
            self.assertEqual(loaded, set())
            self.assertEqual(list((Path(root) / 'Library/LaunchAgents').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
