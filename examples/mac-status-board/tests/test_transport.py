"""Focused localhost protocol tests; no device or credential file is touched."""
import importlib.util
import json
import pathlib
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('transport', ROOT/'host/telemetry_server.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
KEY = b'test-key-only-never-a-device-secret'
NONCE = '0123456789abcdef0123456789abcdef'
SNAPSHOT = {'schema_version': 2, 'unix_time': 1800000000, 'system': {'cpu': 42, 'host': 'test-mac', 'password': 'must disappear'}, 'tasks': {'codex': {'state':'ok', 'items':[{'id':'a','title':'safe title','body':'SECRET','token':'SECRET'}]}}, 'quotas': [{'id':'team','name':'Team','state':'ok','windows':[{'name':'week','label':'7 days','remaining_percent':50,'resets_at':1800000300,'secret':'SECRET'}],'credentials':'SECRET'}], 'secret':'SECRET'}


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.server = t.TelemetryServer(lambda: SNAPSHOT, KEY, host='127.0.0.1', port=0, advertise=False, interval=.02).start()
        self.sockets = []

    def tearDown(self):
        for connection in self.sockets:
            connection.close()
        self.server.close()

    def client(self, nonce=NONCE):
        connection = socket.create_connection(('127.0.0.1', self.server.port), timeout=2)
        self.sockets.append(connection)
        reader = t.FrameReader(connection)
        connection.sendall(t.encode_frame({'v':1,'type':'hello','nonce':nonce}))
        challenge = reader.read()
        self.assertEqual(challenge['proof'],t.proof(KEY,'host',nonce,challenge['nonce']))
        return connection, reader, challenge

    def test_mutual_auth_snapshot_ack_and_disconnect(self):
        connection, reader, challenge = self.client()
        self.assertFalse(self.server.active_client())
        connection.sendall(t.encode_frame({'type':'auth','proof':t.proof(KEY,'board',NONCE,challenge['nonce'])}))
        snapshot = reader.read()
        self.assertEqual(snapshot['data']['system']['cpu'],42)
        self.assertNotIn('SECRET',json.dumps(snapshot))
        self.assertFalse(self.server.active_client())  # Auth alone cannot suppress push.
        connection.sendall(t.encode_frame({'type':'ack','seq':snapshot['seq']}))
        deadline=time.monotonic()+1
        while not self.server.active_client() and time.monotonic()<deadline:time.sleep(.005)
        self.assertTrue(self.server.active_client())
        connection.shutdown(socket.SHUT_RDWR)
        deadline=time.monotonic()+1
        while self.server.active_client() and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.server.active_client())

    def test_wrong_auth_and_replay_get_no_snapshot(self):
        connection, reader, challenge = self.client()
        old_proof=t.proof(KEY,'board',NONCE,challenge['nonce'])
        connection.sendall(t.encode_frame({'type':'auth','proof':'0'*64}))
        with self.assertRaises(EOFError):reader.read()
        connection2, reader2, challenge2 = self.client()
        self.assertNotEqual(challenge['nonce'],challenge2['nonce'])
        connection2.sendall(t.encode_frame({'type':'auth','proof':old_proof}))
        with self.assertRaises(EOFError):reader2.read()
        self.assertFalse(self.server.active_client())

    def test_udp_offer_uses_peer_and_hmac(self):
        udp=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);self.sockets.append(udp);udp.settimeout(2)
        udp.sendto(t.encode_frame({'v':1,'type':'probe','nonce':NONCE,'ip':'203.0.113.1'}),('127.0.0.1',self.server.port))
        raw,peer=udp.recvfrom(1024);offer=json.loads(raw)
        self.assertEqual(peer[0],'127.0.0.1')
        self.assertEqual(offer['port'],self.server.port)
        self.assertEqual(offer['proof'],t.proof(KEY,'offer',NONCE,offer['nonce'],self.server.port))
        self.assertNotIn(KEY,raw)

    def test_bounds_and_whitelist(self):
        clean=t.sanitize_snapshot(SNAPSHOT)
        self.assertNotIn('SECRET',json.dumps(clean));self.assertNotIn('password',clean['system'])
        self.assertEqual(clean['quotas'][0]['windows'][0]['remaining_percent'],50)
        self.assertTrue(t.lan_peer('192.168.5.4'));self.assertTrue(t.lan_peer('172.31.2.3'))
        for ip in ('8.8.8.8','100.64.1.1','169.254.1.1','192.0.2.4','224.0.0.1','172.32.1.1'):self.assertFalse(t.lan_peer(ip))
        with self.assertRaises(ValueError):t.encode_frame({'data':'x'*t.MAX_FRAME})
        connection=socket.create_connection(('127.0.0.1',self.server.port),timeout=2);self.sockets.append(connection)
        connection.sendall(b'x'*(t.MAX_FRAME+1))
        self.assertEqual(connection.recv(10),b'')

    def test_ack_must_match_last_sequence(self):
        connection,reader,challenge=self.client()
        connection.sendall(t.encode_frame({'type':'auth','proof':t.proof(KEY,'board',NONCE,challenge['nonce'])}))
        snapshot=reader.read()
        connection.sendall(t.encode_frame({'type':'ack','seq':snapshot['seq']+1}))
        with self.assertRaises(EOFError):reader.read()
        self.assertFalse(self.server.active_client())

    def test_close_stops_sockets_threads(self):
        self.client()
        self.server.close()
        self.assertFalse(any(thread.is_alive() for thread in self.server.threads))
        self.assertFalse(self.server.active_client())

    def test_fresh_ack_gate_and_maximum_unicode_payload(self):
        with self.server.lock:self.server.last_ack[object()]=time.monotonic()-6
        self.assertFalse(self.server.active_client())
        with self.server.lock:self.server.last_ack.clear()
        value=json.loads(json.dumps(SNAPSHOT))
        task={name:'界'*500 for name in ('id','title','project','model','status')}
        value['tasks']={name:{'items':[task]*12,'total':12} for name in ('codex','opencode')}
        value['quotas']=value['quotas']*8
        clean=t.sanitize_snapshot(value)
        encoded=t.encode_frame({'v':1,'type':'snapshot','seq':1,'data':clean})
        self.assertLessEqual(len(encoded),t.MAX_FRAME+1)
        self.assertEqual(clean['tasks']['codex']['total'],12)

    def test_epoch_fields_use_integer_seconds_on_both_wire_transports(self):
        value=json.loads(json.dumps(SNAPSHOT))
        value['unix_time']=1795708800.0
        value['system']['sampled_at']=1795708800.875
        value['tasks']['codex']['items'][0].update(started_at=1793030400.0,ended_at=1795046400.0,updated_at=1795708800.0)
        value['quotas'][0].update(sampled_at=1793030400.0,valid_until=1795708800.0)
        value['quotas'][0]['windows'][0].update(resets_at=1795708800.0,expires_at=1795046400.0)
        data=t.sanitize_snapshot(value)  # Shared by TCP snapshot and legacy HTTP push.
        self.assertEqual(data['system']['sampled_at'],1795708800)
        timestamps=[data['unix_time'],data['system']['sampled_at'],*data['tasks']['codex']['items'][0].values(),data['quotas'][0]['sampled_at'],data['quotas'][0]['valid_until'],data['quotas'][0]['windows'][0]['resets_at'],data['quotas'][0]['windows'][0]['expires_at']]
        for stamp in timestamps:
            if isinstance(stamp,(int,float)):
                self.assertIs(type(stamp),int)
        self.assertEqual(data['quotas'][0]['windows'][0]['resets_at'],1795708800)
        self.assertEqual(data['quotas'][0]['windows'][0]['expires_at'],1795046400)
        self.assertEqual(data['tasks']['codex']['items'][0]['started_at'],1793030400)

    def test_native_hmac_cross_vectors(self):
        source = r'''
#include "board_protocol.hpp"
#include <iostream>
int main(){std::cout<<board::protocol::proof("test-key-only-never-a-device-secret","host","0123456789abcdef0123456789abcdef","fedcba9876543210fedcba9876543210")<<"\n";std::cout<<board::protocol::hmac(std::string(100,'k'),std::string(200,'m'))<<"\n";return !board::protocol::nonceValid("0123456789abcdef0123456789abcdef")||board::protocol::nonceValid("invalid");}
'''
        with tempfile.TemporaryDirectory() as directory:
            path=pathlib.Path(directory);(path/'test.cpp').write_text(source)
            subprocess.run(['c++','-std=c++17','-I'+str(ROOT/'include'),str(path/'test.cpp'),'-o',str(path/'test')],check=True,capture_output=True)
            output=subprocess.check_output([str(path/'test')],text=True).splitlines()
        self.assertEqual(output[0],t.proof(KEY,'host',NONCE,'fedcba9876543210fedcba9876543210'))
        self.assertEqual(output[1],t.hmac.new(b'k'*100,b'm'*200,t.hashlib.sha256).hexdigest())

if __name__=='__main__':unittest.main()
