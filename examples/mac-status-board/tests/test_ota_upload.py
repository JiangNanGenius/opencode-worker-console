"""Mock-only OTA window tests: never contact launchctl, a board, or real tokens."""
import contextlib
import importlib.util
import io
import pathlib
import plistlib
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ota_upload', ROOT/'host/ota_upload.py')
ota = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ota)


class OtaUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = pathlib.Path(self.temp.name)
        self.plist = self.home/'Library/LaunchAgents/com.local.status-board.plist'
        self.plist.parent.mkdir(parents=True)
        self.plist.write_bytes(plistlib.dumps({'Label':ota.AGENT_LABEL}))
        self.firmware=self.home/'firmware.bin';self.firmware.write_bytes(b'firmware-fixture')
        self.token=self.home/'token';self.token.write_text('test-only-secret-not-a-device-key')
        self.argv=[str(self.firmware),'--token-file',str(self.token),'--url','http://board.local']
        self.events=[]
        self.calls=[]
        self.proc=patch.object(ota.subprocess,'run',side_effect=self.run_command).start()
        self.addCleanup(patch.stopall)
        patch.object(ota.pathlib.Path,'home',return_value=self.home).start()
        patch.object(ota.os,'getuid',return_value=501).start()
        patch.object(ota.time,'sleep',side_effect=lambda seconds:self.events.append(('sleep',seconds))).start()
        self.original_handler=object()
        self.registered_handlers=[]
        self.get_handler=patch.object(ota.signal,'getsignal',return_value=self.original_handler).start()
        self.set_handler=patch.object(ota.signal,'signal',side_effect=lambda sig,handler:self.registered_handlers.append((sig,handler))).start()
        self.response=MagicMock();self.response.status=200;self.response.read.return_value=b'Updated; rebooting'
        self.response.__enter__.return_value=self.response
        self.http=patch.object(ota.urllib.request,'urlopen',side_effect=self.upload).start()

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        self.events.append(command[1])
        return subprocess.CompletedProcess(command,0)

    def upload(self, request, **kwargs):
        self.events.append('upload')
        self.assertEqual(kwargs['timeout'],45)
        self.assertEqual(request.full_url,'http://board.local/update')
        self.assertIn(b'firmware-fixture',request.data)
        return self.response

    def invoke(self, paused=True):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            ota.main(self.argv+(['--pause-agent'] if paused else []))
        return output.getvalue()

    def test_pause_upload_restore_fixed_agent_only(self):
        self.invoke()
        self.assertEqual(self.events,['print','bootout',('sleep',5),'upload','bootstrap'])
        self.assertEqual(self.calls,[['launchctl','print','gui/501/com.local.status-board'],['launchctl','bootout','gui/501/com.local.status-board'],['launchctl','bootstrap','gui/501',str(self.plist)]])
        self.assertNotIn(self.token.read_text(),repr(self.calls))

    def test_upload_failure_always_restores(self):
        for error in (BrokenPipeError('upload failed'),socket.timeout('upload timeout')):
            with self.subTest(error=type(error).__name__):
                self.events.clear();self.http.side_effect=error
                with self.assertRaises(type(error)):self.invoke()
                self.assertEqual(self.events,['print','bootout',('sleep',5),'bootstrap'])

    def test_wait_failure_always_restores(self):
        with patch.object(ota.time,'sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.invoke()
        self.assertEqual(self.events,['print','bootout','bootstrap'])
        self.http.assert_not_called()

    def test_default_path_never_calls_launchctl_or_waits(self):
        self.invoke(paused=False)
        self.assertEqual(self.events,['upload'])
        self.proc.assert_not_called()
        self.get_handler.assert_not_called();self.set_handler.assert_not_called()

    def test_absent_service_does_not_upload_or_change_services(self):
        self.proc.side_effect=subprocess.CalledProcessError(113,['launchctl','print'])
        with self.assertRaises(subprocess.CalledProcessError):self.invoke()
        self.assertEqual(self.proc.call_args[0][0],['launchctl','print','gui/501/com.local.status-board'])
        self.http.assert_not_called()

    def test_mismatched_plist_fails_before_any_command(self):
        self.plist.write_bytes(plistlib.dumps({'Label':'some.other.service'}))
        with self.assertRaises(RuntimeError):self.invoke()
        self.proc.assert_not_called();self.http.assert_not_called()

    def test_restore_failure_is_reported(self):
        def fail_restore(command,**kwargs):
            self.run_command(command,**kwargs)
            if command[1]=='bootstrap':raise subprocess.CalledProcessError(5,command)
        self.proc.side_effect=fail_restore
        with self.assertRaises(subprocess.CalledProcessError):self.invoke()
        self.assertEqual(self.events[-1],'bootstrap')

    def test_sigterm_upload_restores_agent_and_original_handler(self):
        def terminate_upload(*args,**kwargs):
            self.events.append('upload')
            signum,handler=self.registered_handlers[-1]
            self.assertEqual(signum,ota.signal.SIGTERM)
            handler(signum,None)  # Invoke the captured mock registration; no real signal.
        self.http.side_effect=terminate_upload
        with self.assertRaises(SystemExit) as caught:self.invoke()
        self.assertEqual(caught.exception.code,128+ota.signal.SIGTERM)
        self.assertEqual(self.events,['print','bootout',('sleep',5),'upload','bootstrap'])
        self.assertEqual(self.registered_handlers[-1],(ota.signal.SIGTERM,self.original_handler))

    def test_failed_bootout_does_not_bootstrap_and_restores_handler(self):
        def fail_bootout(command,**kwargs):
            self.run_command(command,**kwargs)
            if command[1]=='bootout':raise subprocess.CalledProcessError(5,command)
        self.proc.side_effect=fail_bootout
        with self.assertRaises(subprocess.CalledProcessError):self.invoke()
        self.assertEqual(self.events,['print','bootout'])
        self.http.assert_not_called()
        self.assertEqual(self.registered_handlers[-1],(ota.signal.SIGTERM,self.original_handler))

    def test_server_reply_cannot_print_token(self):
        self.response.read.return_value=self.token.read_bytes()
        output=self.invoke(paused=False)
        self.assertNotIn(self.token.read_text(),output)
        self.assertIn('[redacted]',output)


if __name__=='__main__':unittest.main()
